# SPCS Workbench

Multi-user RStudio / JupyterLab on Snowpark Container Services. One portal URL; each session is its own service with caller's rights.

## Install

You need about 15 minutes of hands-on time; the installer then runs on its own for **1 to 2 hours** the first time (it builds and uploads five container images).

### What you need
| | How to check | If missing |
|---|---|---|
| A Snowflake user that can use **ACCOUNTADMIN** | Snowsight > your name > Switch role | Ask your Snowflake account admin |
| A Mac, Linux or Windows 10/11 computer with ~20 GB free disk | | |
| **Docker Desktop**, running | `docker info` prints details, not an error | Install from [docker.com](https://www.docker.com/products/docker-desktop/), open it |
| **Snowflake CLI** | `snow --version` | Install from the [Snowflake CLI docs](https://docs.snowflake.com/en/developer-guide/snowflake-cli/installation/installation) (macOS: `brew install snowflake-cli`) |
| **python3** (Mac/Linux only) | `python3 --version` | macOS: `xcode-select --install` |

### Steps
1. **Get the files.** Download this repository (green **Code** button > **Download ZIP**) and unzip it, or `git clone` it. Open a terminal in the `workbench` folder:
   ```
   cd path/to/repo/workbench
   ```
2. **Connect the Snowflake CLI to your account** (skip if `snow connection list` already shows one):
   ```
   snow connection add
   ```
   Answer the prompts:
   - **Connection name:** `default`.
   - **Account:** your account identifier (Snowsight > your name > Account > *View account details*, e.g. `myorg-myaccount`).
   - **User:** your Snowflake user.
   - **Authenticator:** `externalbrowser` for SSO, or your password.
   - **Role:** `ACCOUNTADMIN`.
   - Leave the other prompts empty.

   Then check it works:
   ```
   snow connection test -c default
   ```
3. **Edit `config.env`** in any text editor. The three settings most installs need:
   - `DATA_DATABASES`: the databases people will analyse, e.g. `"ANALYTICS RESEARCH"`.
   - `FIRST_ADMIN`: your Snowflake user name. Leave it empty to make the user who runs the installer the admin.
   - `USER_ACCESS_ROLES`: optional. Existing roles whose members get Workbench access straight away, e.g. `"DATA_SCIENTIST"`.

   Everything else has working defaults; each line is explained in the file.
4. **Run the installer:**
   - **Mac / Linux:**
     ```
     ./install.sh
     ```
   - **Windows:** open **PowerShell** (Start menu > type *PowerShell*), `cd` to the `workbench` folder, and run:
     ```
     powershell -ExecutionPolicy Bypass -File .\install.ps1
     ```
     The `-ExecutionPolicy Bypass` part allows this one script to run; it doesn't change any system setting.
   It prints each step as it goes. Uploading images often says *"upload interrupted, resuming"*; that's normal, and it carries on where it stopped. Keep the computer awake until it finishes.
5. **Open the portal.** The installer ends by printing the URL:
   ```
   Workbench portal:   https://xxxx-myorg-myaccount.snowflakecomputing.app
   ```
   Sign in with your normal Snowflake login. You're the admin.
6. **Give people access.** In the portal go to **Admin > Users**, enter a user name (or a role, which covers everyone in it), and pick a level:
   - **User:** runs sessions.
   - **Auditor:** read-only administration.
   - **Admin:** manages everything.
7. **Approve packages** that users may install (**Packages**), and adjust settings such as the idle timeout and session sizes (**Admin > Settings**).

### If something goes wrong
- **The installer stops with an `ERROR`:** the message says what's wrong. Fix it and run `./install.sh` again. Re-running is always safe, and finished steps are quick.
- **"Could not connect ... as ACCOUNTADMIN":** run `snow connection test -c default`. The user must have the ACCOUNTADMIN role.
- **"Docker is installed but not running":** open Docker Desktop and wait until it says it's running.
- **Upload keeps failing or is very slow:** the images total several GB.
  - Use a fast, wired network.
  - In Docker Desktop > Settings > Docker Engine, add `"max-concurrent-uploads": 1` to the JSON, then click **Apply & restart**.
  - Re-run `./install.sh`. Finished uploads are kept.
- **Re-running without rebuilding images** (for example after changing `DATA_DATABASES` or `SESSION_EGRESS_HOSTS`): `./install.sh --skip-images`, or on Windows `.\install.ps1 -SkipImages`.
- **Windows:**
  - **"snow is not recognized":** close PowerShell and open a new window after installing the Snowflake CLI, so it picks up the new PATH.
  - **Image builds fail on Windows:** Docker Desktop must use **Linux containers** (the default). Check the whale icon menu.
  - **"running scripts is disabled":** include `-ExecutionPolicy Bypass` as shown above.
  - Every command in this README has a Windows form: swap `./install.sh` for `.\install.ps1` and `./uninstall.sh` for `.\uninstall.ps1`. Options become `-SkipImages`, `-Purge` and `-Config file.env`.

### Upgrading
Get the new version of the files, set a new `IMAGE_TAG` in `config.env` (e.g. `v2`), and run `./install.sh`. Existing sessions keep working; new sessions use the new images.

### Uninstalling
```
./uninstall.sh            # removes the app (services, pools, integrations, roles); keeps user files and the audit log
./uninstall.sh --purge    # also deletes user files, the audit log, settings and images
```
It asks you to type the database name to confirm. Your data databases and the warehouse are never touched.

### What gets created
| Object | Default name | Purpose |
|---|---|---|
| Database | `WORKBENCH_APP` | Schemas `WORKBENCH` (app tables, user files, audit log) and `SPCS` (images) |
| Warehouse | `WORKBENCH_WH` (X-Small) | App queries and the hourly audit task |
| Compute pools | `WORKBENCH_PORTAL_POOL` (1 small node, always on), `WORKBENCH_SESSION_POOL` (1-4 nodes, scales with sessions) | Run the portal and the sessions |
| Integrations | `WORKBENCH_PKG_EGRESS` (+ `WORKBENCH_SESSION_EGRESS` if hosts are set) | Package downloads for the portal; optional data hosts for sessions |
| Roles | `WORKBENCH_OWNER`, `WORKBENCH_USER` / `_AUDITOR` / `_ADMIN` | Owns the app; access levels |
| Services | `WORKBENCH.PORTAL`, plus one `WB_<id>` per session | The portal and each user's session |

**Costs:**
- The portal pool runs continuously on one CPU_X64_XS node.
- The session pool keeps one node running and adds nodes as sessions start, up to `SESSION_POOL_MAX_NODES`.
- Idle sessions suspend after 4 hours (Admin > Settings).

### Files
| File | Role |
|---|---|
| `config.env` | Your settings (the only file you edit) |
| `install.sh` / `uninstall.sh` | Install or upgrade; remove (Mac / Linux) |
| `install.ps1` / `uninstall.ps1` | The same for Windows PowerShell |
| `prereqs.sql`, `session_egress.sql`, `setup.sql`, `audit.sql`, `audit_retention.sql`, `portal.sql` | SQL the installer runs, in that order. Values in `<% ... %>` come from `config.env` |
| `portal/`, `images/` | Portal app and session images |
| `examples/` | Quick connection tests for RStudio (`snowflake_test.R`) and JupyterLab (`snowflake_test.py`) |

## Access levels
Workbench access is three Snowflake roles (prefix `ACCESS_ROLE_PREFIX` in `config.env`), owned by the app role (`OWNER_ROLE`) so that Workbench admins can grant them from the portal without ACCOUNTADMIN.

| Role | Can |
|---|---|
| `WORKBENCH_USER` | Use the portal, run their own sessions, browse and request packages |
| `WORKBENCH_AUDITOR` | All of the above, plus read-only views of all sessions, the query audit, the admin log, users and settings |
| `WORKBENCH_ADMIN` | All of the above, plus grant and revoke access, suspend or delete any session, approve or deny packages, and change settings |

The roles are hierarchical: ADMIN includes AUDITOR, which includes USER. You can grant a level to a user or to a role, such as an IdP-synced group. Every admin action is written to `ADMIN_AUDIT`. The portal won't let you remove the last admin.

## Curated packages
```
admin approves in portal ──> portal mirrors package + dependencies (Posit PM binaries / PyPI wheels)
                              into a local repo, persisted to @PKG_REPO
sessions ──> http://portal:8081 (private endpoint) ──> install.packages() / pip install
sessions ──X  CRAN / Bioconductor / PyPI (no egress to package hosts)
```
- **Shop:** **Packages** searches CRAN, Bioconductor and PyPI, and shows each package's description, license, dependencies and any OS libraries it needs. Users click **Request**; admins click **Approve** or **Deny**, with an optional reason that users see.
- **Mirroring:** approving a package mirrors it and its whole dependency tree for every R image, using the same dated Posit snapshot the image was built from, and for Python 3.12. If one of the package's dependencies is denied, the package shows as **blocked**.
- **Versions:** the package page lists every published version and which images it's available for. Admins tick the versions users may install; **Latest** means the newest version in each image's snapshot. Users can request a specific version.
  - R: `install.packages("pkg")` installs the default version: the snapshot version if Latest is approved, otherwise the highest approved version. `install.packages("pkg", version = "x.y.z")` installs any approved version, and `wb_versions("pkg")` lists them.
  - Python: every approved version is in the index, so use `pip install pkg==x.y.z`. Admins can also approve specifiers such as `>=2.0`.
  - Bioconductor: each image's version is fixed by its Bioconductor release, so pinning isn't offered.
  - A version published after an image's snapshot date isn't available for that image; the mirror status lists it per image.
- **Enforcement:** session egress (`<prefix>_SESSION_EGRESS`) has no package hosts, so unapproved packages can't be installed. In R, `install.packages()` also prints a short "request it in the portal" message.
- **Preinstalled packages:** the packages baked into the images are listed under **Included**. Changing them means editing `images/*/Dockerfile` and rebuilding.
- **Limits:**
  - Packages that need extra OS libraries (for example `sf` needs GDAL) install but won't load until those libraries are added to the image.
  - Dependencies of a pinned R version use the image snapshot's versions; a very old pin can need older dependencies than the snapshot has.
  - Denying a package stops new installs; copies users already installed in `~/work` stay.
- **Portal egress** (`<prefix>_PKG_EGRESS`) allows: `p3m.dev`, `rspm-sync.rstudio.com` (Posit's binary CDN), `pypi.org` and `files.pythonhosted.org`.

## Customizing
- **Names, data access, egress hosts, pool size:** `config.env`, then re-run `./install.sh` (add `--skip-images` if only settings changed).
- **Session egress hosts** (`SESSION_EGRESS_HOSTS`): hosts that R/Python code may call directly, such as an S3 bucket. Don't add package hosts, or users can install anything.
- **Runtime settings:** **Admin > Settings**: which roles users may pick per session, the idle timeout, and the sizes offered.
- **Preinstalled packages:** edit `images/*/Dockerfile`, bump `IMAGE_TAG`, re-run `./install.sh`.
- **New R version:** add it to `R_TARGETS` in `portal/pkgrepo.py` (Ubuntu codename, Posit snapshot date, Bioconductor release), to `R_VERSIONS` in `portal/app.py`, and to the R version loop in `install.sh`.

## Notes
- **Second sign-in:** opening a session goes to its own URL, which needs one sign-in (silent with SSO). This is required: SPCS caller tokens only work in the service that received them.
- **Deleting a session:** the service and its block volume are dropped; the `SESSIONS` row is kept (`STATE='DELETED'`) for auditing. User files in `@USER_HOMES/<user>/` are kept, because all of that user's sessions share them.
- **Idle sessions:** suspended after the idle timeout (240 minutes by default). Pool nodes scale down when idle (`AUTO_SUSPEND_SECS`).
