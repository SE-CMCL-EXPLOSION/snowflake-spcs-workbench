# Quick Snowflake connection test (JupyterLab session). Runs as you, with the role picked for
# this session (or pass role="SOME_ROLE").
import pandas as pd
from sfwb import sf_connect, sf_session

conn = sf_connect()
cur = conn.cursor()
cur.execute("SELECT CURRENT_USER(), CURRENT_ROLE(), CURRENT_WAREHOUSE(), CURRENT_DATABASE()")
print(cur.fetchone())

# Which databases can this role see?
cur.execute("SHOW DATABASES")
print(pd.DataFrame(cur.fetchall(), columns=[c[0] for c in cur.description])[["name", "owner"]])

# A query computed in Snowflake, returned as a pandas DataFrame
cur.execute("""SELECT UNIFORM(1, 6, RANDOM()) + UNIFORM(1, 6, RANDOM()) AS DICE
               FROM TABLE(GENERATOR(ROWCOUNT => 10000))""")
df = cur.fetch_pandas_all()
print(df["DICE"].value_counts().sort_index())
conn.close()

# Same idea with Snowpark (the work stays in Snowflake until .show() / .to_pandas())
session = sf_session()
session.sql("SELECT CURRENT_VERSION() AS VERSION").show()
session.close()
