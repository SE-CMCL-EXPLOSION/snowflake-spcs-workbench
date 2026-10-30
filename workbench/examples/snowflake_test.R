# Quick Snowflake connection test (RStudio session). Runs as you, with the role picked for
# this session (or pass role = "SOME_ROLE").
con <- sf_connect()

# Who am I?
DBI::dbGetQuery(con, "SELECT CURRENT_USER() AS user, CURRENT_ROLE() AS role,
                             CURRENT_WAREHOUSE() AS warehouse, CURRENT_DATABASE() AS db")

# Which databases can this role see?
dbs <- DBI::dbGetQuery(con, "SHOW DATABASES")
print(dbs[, c("name", "owner")])

# A query computed in Snowflake (works for any role), returned as an R data frame
sim <- DBI::dbGetQuery(con, "
  SELECT UNIFORM(1, 6, RANDOM()) + UNIFORM(1, 6, RANDOM()) AS dice
  FROM TABLE(GENERATOR(ROWCOUNT => 10000))")
library(ggplot2)
print(ggplot(sim, aes(factor(DICE))) + geom_bar(fill = "#29B5E8") +
        labs(x = "sum of two dice", y = "count", title = "10,000 rolls, simulated in Snowflake"))

DBI::dbDisconnect(con)
