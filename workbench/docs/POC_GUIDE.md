# Snowflake Workbench POC Guide

**For:** data engineering teams (admins) and data scientists / researchers (users)
**Covers:** how it works · Posit vs. Workbench · POC setup checklist · admin guide · data ingestion and storage · user guide

---

## 1. How it works

Snowflake Workbench gives each user their own RStudio or JupyterLab session. Everything runs **inside your Snowflake account** on Snowpark Container Services (SPCS), Snowflake's managed container platform. There are no servers, VMs or Kubernetes for you to run.

```
                 Your Snowflake account
 ┌────────────────────────────────────────────────────────────────┐
 │                                                                │
 │  Portal (1 small, always-on container)                         │
 │   - sign-in, start/stop sessions, Admin, Packages              │
 │   - curated package repository (R + Python)                    │
 │                                                                │
 │  Sessions (one container per user session, autoscaling pool)  │
 │   - RStudio (R 4.3.3 / 4.4.3 / 4.5.1) or JupyterLab (Py 3.12)  │
 │   - queries run AS THE SIGNED-IN USER, with their roles        │
 │   - ~/work saved to a Snowflake stage (survives restarts)      │
 │                                                                │
 │  Your data: tables, views, stages (incl. S3 via external stage)│
 └────────────────────────────────────────────────────────────────┘
        ▲ browser, Snowflake sign-in (SSO)
     Users
```

What matters for security and governance:

- **No separate sign-in system.** Users sign in with their normal Snowflake login (your SSO). Access to the app is a Snowflake role.
- **Caller's rights.** Each query from RStudio or Jupyter runs as the person who started the session, under the role they picked. Snowflake applies the same grants, masking policies and row access policies as in Snowsight or any BI tool. There are no shared service accounts and no database passwords in the code.
- **No cloud credentials in sessions.** S3 data is read through a Snowflake stage, so users never handle AWS keys.
- **Curated packages.** Sessions can't reach CRAN, Bioconductor or PyPI directly. Users install from a repository of packages that admins have approved.
- **Full audit trail.** Every query from a session is tagged with the session, user and IDE. It's kept permanently in a Snowflake table, even after the session is deleted.

**Where things live**

| Object | Name (defaults from `config.env`) | Holds |
|---|---|---|
| App database | `APP_DATABASE` | Schema `WORKBENCH` (app tables, user files, audit log) and `SPCS` (container images) |
| Compute pools | `<prefix>_PORTAL_POOL`, `<prefix>_SESSION_POOL` | The machines the portal and sessions run on |
| Access roles | `WORKBENCH_USER`, `WORKBENCH_AUDITOR`, `WORKBENCH_ADMIN` | Who can use and administer the app |
| Owner role | `OWNER_ROLE` | Owns the app's objects; only app administrators need it |

---

## 2. Posit Workbench vs. Snowflake Workbench

The user-facing experience is the same IDE in both: RStudio or JupyterLab in a browser. The differences are in how it's run and governed.

| | Posit Workbench (Native App) | Snowflake Workbench (this app) |
|---|---|---|
| **Maturity / support** | Commercial product, vendor-supported, long track record | Built for this POC; you own the code (provided as a repository) |
| **IDEs** | RStudio, JupyterLab, VS Code, Positron | RStudio (3 R versions), JupyterLab (Python 3.12) |
| **Runs on** | Snowflake (Native App on SPCS) | Snowflake (SPCS in your account) |
| **Sign-in** | Snowflake / SSO | Snowflake / SSO |
| **Data access** | Via Snowflake connection | Caller's rights: the user's own roles, masking and row policies, automatically |
| **Packages** | Posit Package Manager (separate product) | Built-in curated repository: users request, admins approve per version |
| **Audit** | Posit and Snowflake logs | Every session query in a permanent Snowflake table, linked to user and session |
| **Admin** | Posit admin tools plus SQL | Web Admin page (users, sessions, packages, settings) plus SQL |
| **Customizing** | Through Posit's configuration | Full source code: images, packages and behaviour can be changed |
| **Cost** | Posit license plus Snowflake compute | Snowflake compute only |

**Suggested POC approach:** run both side by side for the same users, with the same roles and data. Give users both URLs and ask them to compare. Because data access is decided by Snowflake roles in both cases, users see exactly the same data in each. Only the tool experience differs.

---

## 3. POC setup checklist

Do these in order. Section 4 has the details for each step.

| # | Step | Who | Done when |
|---|---|---|---|
| 1 | Install the Workbench (`./install.sh`) | Data Eng | The installer prints the portal URL |
| 2 | Make sure POC users exist in Snowflake (SSO/SCIM) | Data Eng / IT | Each user can sign in to Snowsight |
| 3 | Create the POC database and schemas (`RAW`, `CURATED`, `SANDBOX`) | Data Eng | Schemas exist |
| 4 | Connect S3: storage integration and external stage | Data Eng + AWS admin | `LIST @stage` shows files |
| 5 | Load or define the POC tables (projects, samples, results, with S3 pointers) | Data Eng | Tables queryable |
| 6 | Create the researcher role and grant it the data | Data Eng | Section 4.3 checks pass |
| 7 | Give the researcher role Workbench access | Data Eng | Users see the portal |
| 8 | Add the POC database to `DATA_DATABASES` (re-run installer with `--skip-images` if changed) | Data Eng | A test query from a session works |
| 9 | Approve the R / Python packages users will need | Data Eng / CompBio lead | Packages show **READY** |
| 10 | Document the data for users (comments, tags) | Data Eng | Tables have descriptions |
| 11 | Smoke test as a researcher (Section 6) | Data Eng | Start a session, query, read an S3 file, write a result |
| 12 | Share the portal URL (and the Posit URL) with users | PMO | Users invited |

---

## 4. Admin guide

### 4.1 Access starts in Snowflake

Everything a user can do is decided in Snowflake, not in the app:

1. **The user exists in Snowflake.** Provision users from your identity provider (Okta, Entra ID…) with SAML for sign-in and, ideally, SCIM for provisioning. SCIM also creates your IdP groups as Snowflake roles automatically.
2. **The user has a role that grants the data.** For example, `RESEARCH_POC_RESEARCHER` (Section 4.3).
3. **That role (or the user) has Workbench access.** `WORKBENCH_USER` is granted to it.

The Workbench Admin page is just a convenient front end for step 3. Granting in SQL works the same:

```sql
GRANT ROLE WORKBENCH_USER TO ROLE RESEARCH_POC_RESEARCHER;   -- everyone in the role can use the app
GRANT ROLE WORKBENCH_USER TO USER JSMITH;                -- or one person
```

**Access levels**

| Role | Can |
|---|---|
| `WORKBENCH_USER` | Use the portal, run their own sessions, browse and request packages |
| `WORKBENCH_AUDITOR` | All of the above, plus read-only views of all sessions, the query audit, the admin log, users and settings |
| `WORKBENCH_ADMIN` | All of the above, plus grant and revoke access, suspend or delete any session, approve or deny packages, and change settings |

These levels only control **the app**. They grant **no data**. A Workbench admin with no data roles sees no data.

### 4.2 Why `DATA_DATABASES` exists (and why roles still decide)

Sessions run with *restricted caller's rights*. A query only succeeds if **both** of these allow it:

1. **The user's role** allows it. This is normal RBAC, and it decides who sees what.
2. **The app's caller grants** allow it. This is an upper limit on what the app is allowed to do on anyone's behalf.

`DATA_DATABASES` in `config.env` creates those caller grants, one set per listed database. Think of it as **the list of databases the app may touch at all**. It doesn't give anyone access:

| Situation | Result |
|---|---|
| Database listed, user's role has SELECT | Works |
| Database listed, user's role has no access | "does not exist or not authorized", as in Snowsight |
| Database **not** listed, user's role has SELECT | Fails for everyone in the app (still works in Snowsight) |

So access is role-based; `DATA_DATABASES` stops the app from acting outside the POC data, even for a user who holds powerful roles. When you add a database, add it to `DATA_DATABASES` and run `./install.sh --skip-images`.

### 4.3 Role design for the POC

One researcher role is enough for most POCs. Use the template below, then check it as a test user.

```sql
USE ROLE SECURITYADMIN;
CREATE ROLE IF NOT EXISTS RESEARCH_POC_RESEARCHER COMMENT = 'POC researchers: read POC data, write to SANDBOX';

-- Compute
GRANT USAGE ON WAREHOUSE COMPUTE_WH TO ROLE RESEARCH_POC_RESEARCHER;

-- Read curated data (current and future tables/views)
GRANT USAGE ON DATABASE RESEARCH_POC TO ROLE RESEARCH_POC_RESEARCHER;
GRANT USAGE ON SCHEMA RESEARCH_POC.CURATED TO ROLE RESEARCH_POC_RESEARCHER;
GRANT SELECT ON ALL TABLES    IN SCHEMA RESEARCH_POC.CURATED TO ROLE RESEARCH_POC_RESEARCHER;
GRANT SELECT ON FUTURE TABLES IN SCHEMA RESEARCH_POC.CURATED TO ROLE RESEARCH_POC_RESEARCHER;
GRANT SELECT ON ALL VIEWS     IN SCHEMA RESEARCH_POC.CURATED TO ROLE RESEARCH_POC_RESEARCHER;
GRANT SELECT ON FUTURE VIEWS  IN SCHEMA RESEARCH_POC.CURATED TO ROLE RESEARCH_POC_RESEARCHER;

-- Read S3 files through the stage (Section 5)
GRANT USAGE ON STAGE RESEARCH_POC.RAW.S3_DATA TO ROLE RESEARCH_POC_RESEARCHER;
GRANT USAGE ON SCHEMA RESEARCH_POC.RAW TO ROLE RESEARCH_POC_RESEARCHER;
GRANT USAGE ON FILE FORMAT RESEARCH_POC.RAW.TSV_FF TO ROLE RESEARCH_POC_RESEARCHER;

-- Somewhere to write results
GRANT USAGE, CREATE TABLE, CREATE VIEW, CREATE STAGE ON SCHEMA RESEARCH_POC.SANDBOX TO ROLE RESEARCH_POC_RESEARCHER;

-- App access, and assign people
GRANT ROLE WORKBENCH_USER TO ROLE RESEARCH_POC_RESEARCHER;
GRANT ROLE RESEARCH_POC_RESEARCHER TO USER JSMITH;   -- or map an IdP group via SCIM
```

**Optional: sensitive columns and rows.** Masking and row access policies apply automatically in every tool, including RStudio and Jupyter:

```sql
CREATE MASKING POLICY RESEARCH_POC.CURATED.MASK_PHI AS (v STRING) RETURNS STRING ->
  CASE WHEN IS_ROLE_IN_SESSION('RESEARCH_POC_ADMIN') THEN v ELSE '***MASKED***' END;
ALTER TABLE RESEARCH_POC.CURATED.SAMPLES MODIFY COLUMN PATIENT_ID SET MASKING POLICY RESEARCH_POC.CURATED.MASK_PHI;
```

**Check a role before users arrive:**

```sql
SHOW GRANTS TO ROLE RESEARCH_POC_RESEARCHER;
USE ROLE RESEARCH_POC_RESEARCHER;
SELECT COUNT(*) FROM RESEARCH_POC.CURATED.SAMPLES;   -- should work
LIST @RESEARCH_POC.RAW.S3_DATA;                      -- should list files
```

### 4.4 Running the Workbench day to day

All of this is in the portal under **Admin** (admins can change things; auditors get a read-only view).

| Tab | What you do there |
|---|---|
| **Users** | Grant or revoke User / Auditor / Admin to a user or a role. The app won't let you remove the last admin. |
| **All sessions** | See every session (owner, IDE, size, status, last active). Suspend or delete any of them. |
| **Query audit** | Every query run from sessions: who, when, which session, and the SQL. |
| **Admin log** | Every admin action (grants, package decisions, settings changes). |
| **Settings** | Which roles users may pick per session, the idle timeout (default 240 minutes), and which session sizes are offered. |

**Packages** (portal > Packages):

- **Browse:** search CRAN, Bioconductor and PyPI. Each package shows its description, licence, dependencies, versions and any OS libraries it needs.
- **Approve / deny:** pick which versions are allowed (or **Latest**). Dependencies are mirrored automatically. A package that needs a denied dependency shows as **blocked**.
- **Requests:** users' requests appear under **Approved & requests**, with their reason.
- **Included in images:** packages that are preinstalled and need no approval (for example DBI, odbc, dplyr, dbplyr, ggplot2, tidyr, limma, pandas, scikit-learn, and the Snowflake connector and Snowpark).

**Audit in SQL** (for reporting outside the app):

```sql
SELECT SESSION_OWNER, IDE, START_TIME, ROLE_NAME, QUERY_TEXT, OBJECTS_READ, OBJECTS_WRITTEN
FROM <APP_DATABASE>.WORKBENCH.SESSION_QUERY_LOG
ORDER BY START_TIME DESC;
```

### 4.5 Network access (external access integrations)

Two integrations control what reaches outside Snowflake:

| Integration | Used by | Allows | Change it when |
|---|---|---|---|
| `<prefix>_PKG_EGRESS` | Portal only | Package sources (Posit Package Manager, PyPI) | Never needed for the POC |
| `<prefix>_SESSION_EGRESS` | User sessions | Only the hosts in `SESSION_EGRESS_HOSTS` | Users must call an outside service directly from code |

For S3, use **an external stage** (Section 5), not session egress. It needs no AWS keys, and Snowflake roles control access. Only add hosts to `SESSION_EGRESS_HOSTS` for a real need, and **never** add package sites; that would bypass package approval.

### 4.6 Operations and cost

| What runs | When it costs | How it's kept down |
|---|---|---|
| Portal pool (1 × `CPU_X64_XS`) | Always on | Smallest instance size |
| Session pool (`CPU_X64_M`, up to `SESSION_POOL_MAX_NODES` nodes) | While sessions run | Idle sessions suspend after the idle timeout. The pool suspends after 15 minutes with nothing running. Each node holds several sessions. |
| Warehouse | While queries run | Auto-suspends after 60 s |
| Storage | User files, package repo, images, audit log | Small (GB range) |

Session sizes: **small** (1 CPU, 4 GB), **medium** (2 CPU, 8 GB), **large** (4 CPU, 16 GB). Users can add **fast disk**, a 20 GB scratch volume deleted with the session.

**Watch spend:**

```sql
SELECT DATE_TRUNC('day', START_TIME) AS DAY, COMPUTE_POOL_NAME, SUM(CREDITS_USED) AS CREDITS
FROM SNOWFLAKE.ACCOUNT_USAGE.SNOWPARK_CONTAINER_SERVICES_HISTORY
GROUP BY 1, 2 ORDER BY 1 DESC;
```

Set a resource monitor or budget on the warehouse and pools if you want a hard limit.

**Pause everything** (for example between POC phases):

```sql
ALTER SERVICE <APP_DATABASE>.WORKBENCH.PORTAL SUSPEND;
ALTER COMPUTE POOL <prefix>_SESSION_POOL SUSPEND;   -- suspends sessions first
ALTER COMPUTE POOL <prefix>_PORTAL_POOL SUSPEND;
-- resume: ALTER COMPUTE POOL ... RESUME; ALTER SERVICE ... RESUME;
```

**Upgrade:** set a new `IMAGE_TAG` in `config.env` and run `./install.sh`. New sessions use the new images; user files and settings are kept.

---

## 5. Data ingestion and storage

### 5.1 The pattern for the POC

You described a few tables (projects, samples, results) where some rows **point to large files in S3**. The recommended setup keeps those files in S3 and reads them through Snowflake:

```
S3 bucket ──(storage integration: IAM role trust)──> external stage @RAW.S3_DATA  (directory table on)
                                                            │
RAW / CURATED tables: PROJECTS, SAMPLES, RESULTS            │
   SAMPLES.FILE_PATH = 'expression/S0001.tsv'  ─────────────┘  (relative path inside the stage)
```

**One-time setup (Data Eng + AWS admin):**

```sql
USE ROLE ACCOUNTADMIN;
CREATE STORAGE INTEGRATION RESEARCH_POC_S3_INT
  TYPE = EXTERNAL_STAGE STORAGE_PROVIDER = 'S3' ENABLED = TRUE
  STORAGE_AWS_ROLE_ARN = 'arn:aws:iam::<aws_account_id>:role/<snowflake_access_role>'
  STORAGE_ALLOWED_LOCATIONS = ('s3://<bucket>/<prefix>/');
DESC INTEGRATION RESEARCH_POC_S3_INT;
-- Give STORAGE_AWS_IAM_USER_ARN and STORAGE_AWS_EXTERNAL_ID to your AWS admin
-- to add to the IAM role's trust policy (read-only S3 policy on the bucket/prefix).
GRANT USAGE ON INTEGRATION RESEARCH_POC_S3_INT TO ROLE SYSADMIN;

USE ROLE SYSADMIN;
CREATE SCHEMA IF NOT EXISTS RESEARCH_POC.RAW;
CREATE SCHEMA IF NOT EXISTS RESEARCH_POC.CURATED;
CREATE SCHEMA IF NOT EXISTS RESEARCH_POC.SANDBOX;
CREATE FILE FORMAT RESEARCH_POC.RAW.TSV_FF TYPE = CSV FIELD_DELIMITER = '\t' SKIP_HEADER = 1;
CREATE STAGE RESEARCH_POC.RAW.S3_DATA
  URL = 's3://<bucket>/<prefix>/' STORAGE_INTEGRATION = RESEARCH_POC_S3_INT
  DIRECTORY = (ENABLE = TRUE);
ALTER STAGE RESEARCH_POC.RAW.S3_DATA REFRESH;               -- index existing files
SELECT * FROM DIRECTORY(@RESEARCH_POC.RAW.S3_DATA) LIMIT 10; -- check
```

**Tables:** load the one-time dump with `COPY INTO` (from the same stage or a separate one), or define them as views over staged files. Store each S3 pointer as the **path relative to the stage** (for example `expression/S0001.tsv`). That makes joining to the stage's directory table easy, and it keeps working if the bucket moves.

```sql
-- Which files exist for which samples (and their sizes)
SELECT s.SAMPLE_ID, d.RELATIVE_PATH, d.SIZE, d.LAST_MODIFIED
FROM RESEARCH_POC.CURATED.SAMPLES s
JOIN DIRECTORY(@RESEARCH_POC.RAW.S3_DATA) d ON d.RELATIVE_PATH = s.FILE_PATH;
```

**New files later:** run `ALTER STAGE ... REFRESH` after drops. For automatic updates, configure S3 event notifications to `AUTO_REFRESH` the directory table, or load continuously with Snowpipe.

### 5.2 Where data can live

| Location | Use for | Who controls access |
|---|---|---|
| Tables in `CURATED` | Shared, governed POC data | Roles (SELECT), plus masking and row policies |
| External stage (S3) | Large files that stay in S3 | `USAGE ON STAGE`, through the storage integration (no AWS keys for users) |
| `SANDBOX` schema | Users' results tables and views | Roles (CREATE TABLE); users own what they create |
| `~/work` in a session | Scripts, notebooks, small files | Private to each user. Saved to the Workbench `USER_HOMES` stage, and shared by all of that user's sessions |
| `~/scratch` (fast disk, optional) | Large temporary files during a session | Deleted with the session |

### 5.3 What users can and can't do

| Users can | Because |
|---|---|
| Query any table or view their role can SELECT | Their role |
| Read S3 files through the stage (SQL over files, or download into the session) | `USAGE` on the stage |
| Write results to `SANDBOX` | `CREATE TABLE` on `SANDBOX` |
| Install approved packages | Curated repository |

| Users can't | Because |
|---|---|
| See data their role doesn't grant, or unmasked protected columns | RBAC, masking and row access policies |
| Reach databases outside `DATA_DATABASES` from the app | The app's caller-grant limit (Section 4.2) |
| Read S3 directly with AWS keys, or reach arbitrary internet hosts | Session egress allows only the listed hosts |
| Install unapproved packages | No access to public package sites |
| See other users' sessions or files | Sessions and `~/work` are per user |

### 5.4 Helping users find data

There's no separate catalog to maintain. Snowflake's catalog is the catalog:

- **Describe objects.** Users see these descriptions in Snowsight's catalog and in `SHOW`/`DESCRIBE`:
  ```sql
  COMMENT ON TABLE RESEARCH_POC.CURATED.SAMPLES IS 'One row per sample. FILE_PATH points into @RAW.S3_DATA.';
  COMMENT ON COLUMN RESEARCH_POC.CURATED.SAMPLES.FILE_PATH IS 'Expression file, relative to @RESEARCH_POC.RAW.S3_DATA';
  ```
- **Tag for discovery and governance** (for example domain, sensitivity):
  ```sql
  CREATE TAG RESEARCH_POC.CURATED.DOMAIN;
  ALTER TABLE RESEARCH_POC.CURATED.SAMPLES SET TAG RESEARCH_POC.CURATED.DOMAIN = 'samples';
  ```
- **Users browse** in Snowsight (**Catalog** / search), or from code with `SHOW TABLES IN DATABASE RESEARCH_POC` and `INFORMATION_SCHEMA.TABLES`. Either way they only see objects their role can access.
- **Requesting access:** Snowflake has no built-in request form, so agree a channel for the POC (an email alias, a ticket queue or a Slack channel) and put it in the user guide (Section 6.7). Package requests are built into the portal.

---

## 6. User guide

### 6.1 Sign in and start a session
1. Open the **Workbench portal URL** your admin shared, and sign in with your normal Snowflake login.
2. Click **New session** and choose:
   - **IDE:** RStudio or JupyterLab. For RStudio, also choose the R version.
   - **Role:** which of your Snowflake roles this session uses for data (for example `RESEARCH_POC_RESEARCHER`).
   - **Size:** small, medium or large. Optionally add **fast disk** for big temporary files.
3. Wait for **Running**, then click **Open**. The session opens in its own tab, and the first time you may be asked to sign in once more (usually instant with SSO). That's expected: each session is its own secure service.

Sessions suspend after a period of inactivity (4 hours by default). **Resume** restarts the session, and your `~/work` files are still there. **Delete** removes the session, but your files stay; they're shared by all your sessions.

### 6.2 Connect to Snowflake

There are no passwords, keys or connection strings. You're already signed in.

**R (RStudio)**
```r
con <- sf_connect()                         # uses the role chosen for this session
DBI::dbGetQuery(con, "SELECT CURRENT_USER(), CURRENT_ROLE()")
samples <- DBI::dbGetQuery(con, "SELECT * FROM RESEARCH_POC.CURATED.SAMPLES LIMIT 100")

# dplyr that runs inside Snowflake; only the result comes back
library(dplyr); library(dbplyr)
tbl(con, I("RESEARCH_POC.CURATED.RESULTS")) |> count(PROJECT_ID) |> collect()
```

**Python (JupyterLab)**
```python
from sfwb import sf_connect, sf_session
conn = sf_connect()
df = conn.cursor().execute("SELECT * FROM RESEARCH_POC.CURATED.SAMPLES LIMIT 100").fetch_pandas_all()

session = sf_session()                      # Snowpark: the work stays in Snowflake
session.table("RESEARCH_POC.CURATED.RESULTS").group_by("PROJECT_ID").count().show()
```

Both accept `role=`, `database=`, `schema=` and `warehouse=` to override the defaults.

### 6.3 Find data
- In code: `SHOW TABLES IN DATABASE RESEARCH_POC`, then `DESCRIBE TABLE ...` for columns and descriptions.
- In Snowsight: open **Catalog** and search. You only see what your role can access.

### 6.4 Work with S3 files

S3 files are reached through a **stage**. You don't need AWS credentials.

```sql
-- List files
SELECT RELATIVE_PATH, SIZE FROM DIRECTORY(@RESEARCH_POC.RAW.S3_DATA) WHERE RELATIVE_PATH LIKE 'expression/%';
-- Query a file in place (no download)
SELECT $1, $2, $3 FROM @RESEARCH_POC.RAW.S3_DATA/expression/S0001.tsv (FILE_FORMAT => 'RESEARCH_POC.RAW.TSV_FF');
```

From R, run those through `DBI::dbGetQuery(con, "...")`. Querying files in place is best for large or many files, because Snowflake does the scanning.

To **download a file into your session** (for tools that need a local file), use Python:
```python
session = sf_session()
session.file.get("@RESEARCH_POC.RAW.S3_DATA/expression/S0001.tsv", "/home/jovyan/scratch/")
```

### 6.5 Save results
- **Share as a table:**
  - R: `DBI::dbWriteTable(con, DBI::Id(catalog = "RESEARCH_POC", schema = "SANDBOX", table = "MY_RESULTS"), df)`
  - Python: `session.write_pandas(df, "MY_RESULTS", database="RESEARCH_POC", schema="SANDBOX", auto_create_table=True)`
- **Keep code and files:** save them under `~/work`. It persists and appears in all your sessions.

### 6.6 Packages
- **R:**
  - `install.packages("pkg")` installs approved packages.
  - `install.packages("pkg", version = "1.2.3")` installs a specific approved version.
  - `wb_versions("pkg")` lists the approved versions.
- **Python:** `pip install pkg` or `pip install pkg==1.2.3`.
- **Not approved yet?** Go to the portal > **Packages**, search, and click **Request**. You'll see the decision there.

### 6.7 "Why can't I see…?"

| You see | Likely reason | What to do |
|---|---|---|
| `Object ... does not exist or not authorized` | Your role has no access to it, or the database isn't enabled for the Workbench | Check the session role; ask for access via **[request channel]** |
| `***MASKED***` or rounded values | A masking policy protects that column for your role | Expected; ask if you need the raw values |
| Fewer rows than expected | A row access policy (for example consent or contract rules) | Expected; ask your admin if it looks wrong |
| `Not approved in the Workbench repository` | The package isn't approved | Request it under **Packages** |
| Session asks you to sign in again | Normal the first time you open each session | Sign in; it's remembered |

**Your activity:** each session's **Activity** button shows the queries it ran.

## Snowflake documentation

### Snowpark Container Services (the platform the workbench runs on)

- [Snowpark Container Services overview](https://docs.snowflake.com/en/developer-guide/snowpark-container-services/overview)
- [Working with services](https://docs.snowflake.com/en/developer-guide/snowpark-container-services/working-with-services)
- [Working with compute pools](https://docs.snowflake.com/en/developer-guide/snowpark-container-services/working-with-compute-pool)
- [Monitoring services](https://docs.snowflake.com/en/developer-guide/snowpark-container-services/monitoring-services)
- [Snowpark Container Services costs](https://docs.snowflake.com/en/developer-guide/snowpark-container-services/accounts-orgs-usage-views)
- [SNOWPARK_CONTAINER_SERVICES_HISTORY view](https://docs.snowflake.com/en/sql-reference/account-usage/snowpark_container_services_history)

### Caller's rights and DATA_DATABASES

- [Tutorial: service that uses caller's rights](https://docs.snowflake.com/en/developer-guide/snowpark-container-services/tutorials/advanced/tutorial-7-callers-rights)
- [GRANT CALLER](https://docs.snowflake.com/en/sql-reference/sql/grant-caller)
- [High-level caller privileges](https://docs.snowflake.com/en/developer-guide/restricted-callers-rights/high-level-caller-privileges)

### Package egress (external access)

- [Creating and using an external access integration](https://docs.snowflake.com/en/developer-guide/external-network-access/creating-using-external-network-access)
- [CREATE EXTERNAL ACCESS INTEGRATION](https://docs.snowflake.com/en/sql-reference/sql/create-external-access-integration)

### Data ingestion and storage (S3)

- [Create an S3 stage](https://docs.snowflake.com/en/user-guide/data-load-s3-create-stage)
- [Manage directory tables](https://docs.snowflake.com/en/user-guide/data-load-dirtables-manage)
- [Refresh directory tables automatically for Amazon S3](https://docs.snowflake.com/en/user-guide/data-load-dirtables-auto-s3)
- [Query data in staged files](https://docs.snowflake.com/en/user-guide/querying-stage)
- [Query metadata for staged files](https://docs.snowflake.com/en/user-guide/querying-metadata)
- [Transform data during a load](https://docs.snowflake.com/en/user-guide/data-load-transform)
- [Introduction to external tables](https://docs.snowflake.com/en/user-guide/tables-external-intro)

### Governance and access

- [Understanding column-level security](https://docs.snowflake.com/en/user-guide/security-column-intro)
- [Tag-based masking policies](https://docs.snowflake.com/en/user-guide/tag-based-masking-policies)
- [Attribute-based access control (ABAC) using tag-based policies](https://docs.snowflake.com/en/user-guide/tag-based-policies)
- [Microsoft Entra ID SCIM integration with Snowflake](https://docs.snowflake.com/en/user-guide/scim-azure)

### Data discovery

- [Search for objects with Universal Search](https://docs.snowflake.com/en/user-guide/ui-snowsight-universal-search)
- [Make database objects discoverable in Universal Search](https://docs.snowflake.com/en/user-guide/ui-snowsight/object-visibility-universal-search)
- [Explore and manage database objects in Snowsight](https://docs.snowflake.com/en/user-guide/ui-snowsight-data)

### Installer tooling and client connections

- [Installing Snowflake CLI](https://docs.snowflake.com/en/developer-guide/snowflake-cli/installation/installation)
- [Configuring Snowflake CLI](https://docs.snowflake.com/en/developer-guide/snowflake-cli/connecting/configure-cli)
- [Managing Snowflake connections](https://docs.snowflake.com/en/developer-guide/snowflake-cli/connecting/configure-connections)
- [Snowpark Developer Guide for Python](https://docs.snowflake.com/en/developer-guide/snowpark/python)
- [Creating a session for Snowpark Python](https://docs.snowflake.com/en/developer-guide/snowpark/python/creating-session)
