"""Curated package repository.

Admins approve packages in the portal; this module mirrors them (plus their dependencies) as
Linux binaries into ROOT (local disk, persisted to the @PKG_REPO stage) and builds standard
repository indexes. Sessions install from it via the portal's private `repo` endpoint and have
no egress to public package hosts, so only approved packages can be installed.

Layout under ROOT (local disk; persisted to @PKG_REPO):
  r/<R version>/src/contrib/{PACKAGES,PACKAGES.gz,<pkg>_<ver>.tar.gz}   CRAN + Bioconductor (default versions)
  r/<R version>/archive/<pkg>_<ver>.tar.gz                              other approved versions (CRAN)
  pypi/files/*.whl, pypi/simple/<project>/index.html, pypi/manifest.json  PEP 503
"""
import gzip
import html
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import io
import tempfile
import urllib.parse
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("pkgrepo")
ROOT = Path(os.environ.get("WB_REPO_DIR", "/var/wb-repo"))
P3M = "https://p3m.dev"

# Session images: R version -> (Ubuntu codename, CRAN snapshot the image was built from, Bioconductor)
# Mirroring from the same snapshot keeps approved packages compatible with what's preinstalled.
R_TARGETS = {
    "4.3.3": ("jammy", "2024-04-23", "3.18"),
    "4.4.3": ("noble", "2025-04-10", "3.20"),
    "4.5.1": ("noble", "2025-10-30", "3.22"),
}
PY_VERSION, PY_ABI = "3.12", "cp312"
PY_PLATFORMS = ["manylinux2014_x86_64", "manylinux_2_17_x86_64", "manylinux_2_28_x86_64", "linux_x86_64"]
BASE_R = {"base", "compiler", "datasets", "grDevices", "graphics", "grid", "methods", "parallel",
          "splines", "stats", "stats4", "tcltk", "tools", "utils"}
# Baked into the session images (see images/*/Dockerfile); shown as "Included" in the shop
INCLUDED = {
    "cran": {"DBI", "odbc", "dplyr", "dbplyr", "ggplot2", "tidyr", "paws", "rstudioapi", "BiocManager"},
    "bioc": {"limma"},
    "pypi": {"pandas", "matplotlib", "seaborn", "scikit-learn", "snowflake-connector-python",
             "snowflake-snowpark-python", "aiohttp", "numpy", "jupyterlab"},
}
ECOSYSTEMS = ("cran", "bioc", "pypi")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
PY_SPEC_RE = re.compile(r"^(==|>=|<=|~=|!=|<|>)[0-9][0-9A-Za-z.*+!-]*$")


VER_RE = re.compile(r"^[0-9][0-9A-Za-z.+-]{0,40}$")


def parse_versions(spec):
    """VERSION_SPEC is a comma-separated list of approved versions; "latest" (or empty) means the
    current version in each image's snapshot. PyPI also accepts specifiers like ">=2.0"."""
    toks = [t.strip() for t in (spec or "").split(",") if t.strip()]
    return toks or ["latest"]


def valid_version_token(eco, tok):
    if tok == "latest" or VER_RE.match(tok):
        return True
    return eco == "pypi" and bool(PY_SPEC_RE.match(tok))


def vkey(v):
    return [int(x) if x.isdigit() else -1 for x in re.split(r"[.-]", v)]


def norm_py(name):
    return re.sub(r"[-_.]+", "-", name).lower()


class _Redirects(urllib.request.HTTPRedirectHandler):
    """Remember redirect targets so egress errors name the host that needs allowing."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _Redirects.last = newurl
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_Redirects)


def _get(url, ua="spcs-workbench", timeout=60):
    _Redirects.last = None
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    try:
        with _opener.open(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.URLError as e:
        if _Redirects.last:
            host = urllib.parse.urlparse(_Redirects.last).hostname
            raise RuntimeError(f"{e} (redirected to {host}; allow it in the portal egress rule)") from e
        raise


def _json(url):
    return json.loads(_get(url))


# ---------------------------------------------------------------- catalog (live, via the package APIs + PyPI)
def _p3m_repo(eco):
    return {"cran": "cran", "bioc": "bioconductor", "pypi": "pypi"}[eco]


def _bioc_param(eco):
    return {"bioc_version": R_TARGETS[max(R_TARGETS)][2]} if eco == "bioc" else {}


def search(eco, q, limit=30):
    params = {"name_like": q, "_limit": 100, **_bioc_param(eco)}
    rows = _json(f"{P3M}/__api__/repos/{_p3m_repo(eco)}/packages?" + urllib.parse.urlencode(params))
    ql = q.lower()
    rows.sort(key=lambda r: (r["name"].lower() != ql, not r["name"].lower().startswith(ql), r["name"].lower()))
    return [{"name": r["name"], "version": r.get("version"), "title": (r.get("title") or "").strip()}
            for r in rows[:limit]]


def info(eco, name):
    if eco == "pypi":
        d = _json(f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json")["info"]
        return {"name": d["name"], "version": d["version"], "title": d.get("summary") or "",
                "description": (d.get("summary") or ""), "license": (d.get("license") or "")[:200],
                "url": d.get("home_page") or (d.get("project_urls") or {}).get("Homepage") or "",
                "depends": sorted({re.split(r"[ ;<>=!~\[(]", r, maxsplit=1)[0] for r in (d.get("requires_dist") or [])
                                   if "extra ==" not in r}),
                "sysreqs": []}
    d = _json(f"{P3M}/__api__/repos/{_p3m_repo(eco)}/packages/{urllib.parse.quote(name)}?"
              + urllib.parse.urlencode(_bioc_param(eco)))
    deps = set()
    for f in ("depends", "imports", "linking_to"):
        for part in (d.get(f) or "").split(","):
            n = part.strip().split("(")[0].strip()
            if n and n != "R" and n not in BASE_R:
                deps.add(n)
    sysreqs = []
    try:
        sr = _json(f"{P3M}/__api__/repos/{_p3m_repo(eco)}/packages/{urllib.parse.quote(name)}/sysreqs?"
                   + urllib.parse.urlencode({"distribution": "ubuntu", "release": "24.04", **_bioc_param(eco)}))
        scripts = set(sr.get("install_scripts") or [])
        for dep in sr.get("dependencies") or []:
            scripts.update(dep.get("install_scripts") or [])
        sysreqs = sorted(s.replace("apt-get install -y ", "") for s in scripts)
    except Exception:
        pass
    return {"name": d["name"], "version": d.get("version"), "title": (d.get("title") or "").strip(),
            "description": re.sub(r"\s+", " ", d.get("description") or "").strip()[:1500],
            "license": d.get("license") or "", "url": d.get("url") or "", "depends": sorted(deps), "sysreqs": sysreqs}


def _py_wheel_ok(fn):
    """Is this wheel installable on the session images (CPython 3.12, Linux x86_64)?"""
    if not fn.endswith(".whl"):
        return False
    parts = fn[:-4].split("-")
    if len(parts) < 5:
        return False
    pyt, abi, plat = parts[-3], parts[-2], parts[-1]
    py_ok = any(t in ("py3", "py2.py3", "cp312") or (t.startswith("cp3") and abi == "abi3" and int(t[3:] or 0) <= 12)
                for t in pyt.split("."))
    plat_ok = plat == "any" or ("x86_64" in plat and ("manylinux" in plat or plat == "linux_x86_64"))
    return py_ok and plat_ok and abi in ("none", "abi3", "cp312")


def versions(eco, name, limit=60):
    """Available versions, newest first: [{version, published, targets: {target: bool}}]."""
    if eco == "pypi":
        d = _json(f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json")
        out = []
        for v, files in d["releases"].items():
            if not files or all(f.get("yanked") for f in files):
                continue
            ok = any(_py_wheel_ok(f["filename"]) for f in files if not f.get("yanked"))
            pub = min((f.get("upload_time_iso_8601") or "") for f in files)[:10]
            out.append({"version": v, "published": pub, "targets": {"python " + PY_VERSION: ok}})
        out.sort(key=lambda r: r["published"], reverse=True)
        return out[:limit]
    if eco == "bioc":
        # One version per Bioconductor release, fixed by each R image
        by_ver = {}
        for rver, (_, _, bioc) in R_TARGETS.items():
            try:
                d = _json(f"{P3M}/__api__/repos/bioconductor/packages/{urllib.parse.quote(name)}?"
                          + urllib.parse.urlencode({"bioc_version": bioc}))
                if d.get("version"):
                    by_ver.setdefault(d["version"], set()).add(rver)
            except Exception:
                pass
        return [{"version": v, "published": "", "targets": {"R " + r: r in rs for r in R_TARGETS}}
                for v, rs in sorted(by_ver.items(), key=lambda kv: vkey(kv[0]), reverse=True)]
    d = _json(f"{P3M}/__api__/repos/cran/packages/{urllib.parse.quote(name)}")
    rows = [{"version": d["version"], "published": (d.get("date_publication") or "")[:10]}]
    rows += [{"version": a["version"], "published": (a.get("date_publication") or "")[:10]}
             for a in (d.get("archived") or []) if a.get("version") != d["version"]]
    for r in rows:   # a version exists for an image if it was published by that image's snapshot date
        r["targets"] = {"R " + rv: bool(r["published"]) and r["published"] <= snap
                        for rv, (_, snap, _) in R_TARGETS.items()}
    rows.sort(key=lambda r: vkey(r["version"]), reverse=True)
    return rows[:limit]


# ---------------------------------------------------------------- R mirror
def _parse_packages(text):
    """DCF PACKAGES file -> {name: (fields, raw stanza)}."""
    out = {}
    for block in text.strip().split("\n\n"):
        fields, key = {}, None
        for line in block.splitlines():
            if line[:1] in (" ", "\t") and key:
                fields[key] += " " + line.strip()
            elif ":" in line:
                key, _, v = line.partition(":")
                key = key.strip()
                fields[key] = v.strip()
        if "Package" in fields:
            out[fields["Package"]] = (fields, block.strip())
    return out


def _r_deps(fields):
    names = set()
    for f in ("Depends", "Imports", "LinkingTo"):
        for part in fields.get(f, "").split(","):
            n = part.strip().split("(")[0].strip()
            if n and n != "R" and n not in BASE_R:
                names.add(n)
    return names


def _desc_from_tarball(path, name):
    """DESCRIPTION of a (binary) R package tarball -> (fields, PACKAGES stanza)."""
    with tarfile.open(path, "r:gz") as tf:
        text = tf.extractfile(f"{name}/DESCRIPTION").read().decode("utf-8", "replace")
    fields, _ = _parse_packages(text)[name]
    keep = ("Package", "Version", "Depends", "Imports", "LinkingTo", "Suggests", "License", "NeedsCompilation")
    stanza = "\n".join(f"{k}: {fields[k]}" for k in keep if k in fields)
    return fields, stanza


def sync_r(rver, approved, denied):
    """Mirror approved packages (+ dependency closure) for one R image.
    approved: {name: [version tokens]}. The default version of each package (snapshot version when
    "latest" is approved, else the highest approved pin) goes in the regular index; other approved
    versions go to archive/ for install.packages(pkg, version=). Returns {root: {token: status}}."""
    distro, snap, bioc = R_TARGETS[rver]
    cran = f"{P3M}/cran/__linux__/{distro}/{snap}/src/contrib"
    sources = [f"{P3M}/bioconductor/__linux__/{distro}/latest/packages/{bioc}/bioc/src/contrib", cran]
    index = {}
    for url in sources:                                                     # CRAN wins name clashes
        for name, (fields, raw) in _parse_packages(_get(url + "/PACKAGES", timeout=120).decode()).items():
            index[name] = (url, fields, raw)
    dest = ROOT / "r" / rver / "src" / "contrib"
    arch = ROOT / "r" / rver / "archive"
    dest.mkdir(parents=True, exist_ok=True)
    arch.mkdir(parents=True, exist_ok=True)
    ua = f"R ({rver} x86_64-pc-linux-gnu x86_64 linux-gnu)"   # the server serves the matching binary

    def fetch(url, path):
        if path.exists():
            return
        try:
            path.write_bytes(_get(url, ua=ua, timeout=900))
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def closure(start_deps, root):
        stack, seen, blocked = list(start_deps), set(), []
        while stack:
            n = stack.pop()
            if n in seen or n == root:
                continue
            seen.add(n)
            if n in denied:
                blocked.append(n)
                continue
            if n in index:  # names missing from the index ship with R (recommended pkgs)
                stack.extend(_r_deps(index[n][1]))
        return {n for n in seen if n in index}, blocked

    report, want_deps, main_files, keep_arch = {}, set(), {}, set()
    pinned_main = {}        # root -> (filename, stanza) when its default is a pinned (non-snapshot) version
    for root in sorted(approved):
        toks = approved[root]
        report[root] = {}
        cur = index[root][1]["Version"] if root in index else None
        resolved = []       # (token, version, path-or-None for snapshot, fields)
        for tok in toks:
            ver = cur if tok == "latest" else tok
            if ver is None:
                report[root][tok] = "not available"
                continue
            if ver == cur:
                resolved.append((tok, ver, None, index[root][1]))
                continue
            if index.get(root, (None,))[0] != cran and root in index:
                report[root][tok] = "pins not supported for Bioconductor (version is fixed per R release)"
                continue
            path = arch / f"{root}_{ver}.tar.gz"
            try:
                fetch(f"{cran}/Archive/{root}/{root}_{ver}.tar.gz", path)
                fields, stanza = _desc_from_tarball(path, root)
                resolved.append((tok, ver, path, (fields, stanza)))
            except urllib.error.HTTPError as e:
                report[root][tok] = "not available" if e.code == 404 else f"download failed: {e}"
            except Exception as e:
                report[root][tok] = f"download failed: {e}"
        for tok, ver, path, meta in resolved:
            fields = meta if path is None else meta[0]
            deps, blocked = closure(_r_deps(fields), root)
            if blocked:
                report[root][tok] = "blocked: needs denied package " + ", ".join(sorted(blocked))
                if path:
                    path.unlink(missing_ok=True)
                continue
            want_deps |= deps
            report[root][tok] = f"ok ({ver})" if tok == "latest" else "ok"
        ok = [r for r in resolved if report[root][r[0]].startswith("ok")]
        if not ok:
            continue
        default = next((r for r in ok if r[2] is None), None) or max(ok, key=lambda r: vkey(r[1]))
        for tok, ver, path, meta in ok:
            if path is None:
                main_files[root] = None                          # snapshot version
            elif (tok, ver) == (default[0], default[1]):
                fn = f"{root}_{ver}.tar.gz"
                shutil.copyfile(path, dest / fn)
                pinned_main[root] = (fn, meta[1])
                keep_arch.add(path.name)                         # also keep under archive/ for explicit installs
            else:
                keep_arch.add(path.name)

    # Regular index: snapshot versions of approved roots and all dependencies, then pinned defaults
    entries = {}
    for n in (set(main_files) - set(pinned_main)) | (want_deps - set(pinned_main)):
        url, fields, raw = index[n]
        fn = f"{n}_{fields['Version']}.tar.gz"
        try:
            fetch(f"{url}/{fn}", dest / fn)
            entries[n] = (fn, raw)
        except Exception as e:
            log.warning("R %s: download %s failed: %s", rver, fn, e)
            for t, st in report.get(n, {}).items():
                if st.startswith("ok") and (t == "latest" or t == fields["Version"]):
                    report[n][t] = f"download failed: {e}"
    entries.update(pinned_main)
    keep = {fn for fn, _ in entries.values()}
    for f in dest.iterdir():
        if f.name.endswith(".tar.gz") and f.name not in keep:
            f.unlink()
    for f in arch.iterdir():
        if f.name not in keep_arch and f.name != "VERSIONS":
            f.unlink()
    # "<pkg> <version>" per installable non-default version, read by install.packages(pkg, version=)
    (arch / "VERSIONS").write_text("".join(f"{n.rsplit('_', 1)[0]} {n.rsplit('_', 1)[1][:-7]}\n"
                                           for n in sorted(keep_arch)))
    text = "\n\n".join(entries[n][1] for n in sorted(entries)) + "\n"
    (dest / "PACKAGES").write_text(text)
    (dest / "PACKAGES.gz").write_bytes(gzip.compress(text.encode()))
    return report


# ---------------------------------------------------------------- Python mirror
def _wheel_project(fn):
    return norm_py(fn.split("-")[0])


def _py_req(name, tok):
    if tok == "latest":
        return name
    return name + (tok if PY_SPEC_RE.match(tok) else "==" + tok)


def sync_py(approved, denied):
    """approved: {name: [version tokens]}. Downloads each (root, version) wheel closure once
    (recorded in manifest.json), then publishes files for approved versions minus denied projects.
    Returns {root: {token: status}}."""
    base = ROOT / "pypi"
    files = base / "files"
    files.mkdir(parents=True, exist_ok=True)
    mpath = base / "manifest.json"
    manifest = json.loads(mpath.read_text()) if mpath.exists() else {}
    manifest = {k: v for k, v in manifest.items() if isinstance(v.get("versions"), dict)}  # pre-versioning format
    report = {}
    denied_n = {norm_py(d) for d in denied}
    for name, toks in approved.items():
        key = norm_py(name)
        have = manifest.get(key, {}).get("versions", {})
        entry = {}
        report[name] = {}
        for tok in toks:
            if tok in have:
                entry[tok] = have[tok]
                continue
            with tempfile.TemporaryDirectory() as tmp:
                cmd = [sys.executable, "-m", "pip", "download", "--quiet", "--disable-pip-version-check",
                       "--dest", tmp, "--only-binary=:all:", "--python-version", PY_VERSION,
                       "--implementation", "cp", "--abi", PY_ABI, "--abi", "abi3", "--abi", "none"]
                for pl in PY_PLATFORMS:
                    cmd += ["--platform", pl]
                cmd.append(_py_req(name, tok))
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
                if r.returncode != 0:
                    tail = (r.stderr.strip().splitlines() or ["unknown error"])[-1]
                    report[name][tok] = "failed: " + tail[:300]
                    continue
                got = sorted(os.listdir(tmp))
                for fn in got:
                    if not (files / fn).exists():
                        shutil.copyfile(Path(tmp) / fn, files / fn)
            root_v = next((f.split("-")[1] for f in got if _wheel_project(f) == key), "?")
            entry[tok] = {"files": got, "version": root_v}
        manifest[key] = {"versions": entry}
    approved_n = {norm_py(n): n for n in approved}
    manifest = {k: v for k, v in manifest.items() if k in approved_n}
    keep = set()
    for k, v in manifest.items():
        root = approved_n[k]
        for tok, e in v["versions"].items():
            blocked = sorted({_wheel_project(f) for f in e["files"]} & denied_n - {k})
            if blocked:
                report[root][tok] = "blocked: needs denied package " + ", ".join(blocked)
                continue
            keep.update(e["files"])
            report[root][tok] = f"ok ({e['version']})" if tok == "latest" or not VER_RE.match(tok) else "ok"
    for f in files.iterdir():
        if f.name not in keep:
            f.unlink()
    mpath.write_text(json.dumps(manifest, indent=1))
    _write_simple_index(base, sorted(keep))
    return report


def _write_simple_index(base, wheels):
    simple = base / "simple"
    if simple.exists():
        shutil.rmtree(simple)
    simple.mkdir(parents=True)
    projects = {}
    for fn in wheels:
        projects.setdefault(_wheel_project(fn), []).append(fn)
    links = "".join(f'<a href="{html.escape(p)}/">{html.escape(p)}</a>\n' for p in sorted(projects))
    (simple / "index.html").write_text(f"<!doctype html><html><body>\n{links}</body></html>\n")
    for p, fns in projects.items():
        (simple / p).mkdir()
        a = "".join(f'<a href="../../files/{html.escape(f)}">{html.escape(f)}</a>\n' for f in fns)
        (simple / p / "index.html").write_text(f"<!doctype html><html><body>\n{a}</body></html>\n")


# ---------------------------------------------------------------- persistence (@PKG_REPO stage)
# The working repo lives on the portal's local disk (indexes are rewritten on every sync, which
# stage mounts don't support). After a sync it's pushed to the stage with PUT/REMOVE; on startup
# it's restored with GET, so a portal restart doesn't re-download everything.
SAFE_FILE = re.compile(r"^[A-Za-z0-9._+-]+$")


def _data_dirs():
    return ([f"r/{v}/src/contrib" for v in R_TARGETS] + [f"r/{v}/archive" for v in R_TARGETS]
            + ["pypi/files", "pypi"])


def _children(run_sql, stage, rel):
    """Direct children of <stage>/<rel>/ (LIST returns names prefixed with the stage name)."""
    depth = rel.count("/") + 2
    return {r["name"].split("/")[-1] for r in run_sql(f"LIST {stage}/{rel}/") if r["name"].count("/") == depth}


def persist(run_sql, stage):
    for rel in _data_dirs():
        d = ROOT / rel
        if not d.is_dir():
            continue
        local = {f.name for f in d.iterdir() if f.is_file() and SAFE_FILE.match(f.name)}
        remote = _children(run_sql, stage, rel)
        for name in sorted(local):
            mutable = name.startswith(("PACKAGES", "VERSIONS")) or name.endswith(".json")
            if name in remote and not mutable:
                continue  # package files are immutable by name
            run_sql(f"PUT 'file://{d / name}' {stage}/{rel}/ AUTO_COMPRESS = FALSE OVERWRITE = TRUE")
        for name in sorted(remote - local):
            if SAFE_FILE.match(name):
                run_sql(f"REMOVE {stage}/{rel}/{name}")


def restore(run_sql, stage):
    for rel in _data_dirs():
        d = ROOT / rel
        d.mkdir(parents=True, exist_ok=True)
        for name in _children(run_sql, stage, rel):
            if SAFE_FILE.match(name) and not (d / name).exists():
                run_sql(f"GET {stage}/{rel}/{name} 'file://{d}/'")
    files = ROOT / "pypi" / "files"
    _write_simple_index(ROOT / "pypi", sorted(f.name for f in files.iterdir()) if files.is_dir() else [])


# ---------------------------------------------------------------- all targets
def _rollup(detail):
    ok = [k for k, v in detail.items() if v.startswith("ok")]
    return "READY" if detail and len(ok) == len(detail) else ("PARTIAL" if ok else "FAILED")


def sync_all(policy_rows):
    """policy_rows: PACKAGE_POLICY rows. Returns {(eco, name): (status, detail)} where detail maps
    "<target> <version>" -> "ok" / "ok (<resolved>)" / error."""
    appr = {e: {} for e in ECOSYSTEMS}
    denied = {e: set() for e in ECOSYSTEMS}
    for r in policy_rows:
        if r["STATUS"] == "APPROVED":
            appr[r["ECOSYSTEM"]][r["NAME"]] = parse_versions(r.get("VERSION_SPEC"))
        elif r["STATUS"] == "DENIED":
            denied[r["ECOSYSTEM"]].add(r["NAME"])
    out = {}
    r_appr = {**appr["bioc"], **appr["cran"]}
    r_denied = denied["cran"] | denied["bioc"]
    per = {}
    for rver in R_TARGETS:
        try:
            for root, toks in sync_r(rver, r_appr, r_denied).items():
                for tok, st in toks.items():
                    per.setdefault(root, {})[f"R {rver} {tok}"] = st
        except Exception as e:
            log.exception("R %s sync failed", rver)
            for root, toks in r_appr.items():
                for tok in toks:
                    per.setdefault(root, {})[f"R {rver} {tok}"] = f"sync error: {e}"
    for eco in ("cran", "bioc"):
        for name in appr[eco]:
            detail = per.get(name, {})
            out[(eco, name)] = (_rollup(detail), detail)
    try:
        for name, toks in sync_py(appr["pypi"], denied["pypi"]).items():
            detail = {f"python {PY_VERSION} {t}": st for t, st in toks.items()}
            out[("pypi", name)] = (_rollup(detail), detail)
    except Exception as e:
        log.exception("pypi sync failed")
        for name, toks in appr["pypi"].items():
            out[("pypi", name)] = ("FAILED", {f"python {PY_VERSION} {t}": f"sync error: {e}" for t in toks})
    return out
