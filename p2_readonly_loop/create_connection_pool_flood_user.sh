#!/bin/sh
# One-time setup for the connection-pool-exhaustion fault class.
#
# Found the hard way (2026-07-21): the flood script originally used
# root for all 150 held-open connections -- the SAME user
# mysqld_exporter uses for its own scrape. MySQL reserves a small
# number of extra connection slots specifically for privileged (SUPER)
# users so an admin can still log in during real exhaustion, but that
# protection only works if the monitoring connection uses a DIFFERENT
# identity than whatever is consuming the pool. Confirmed empirically:
# during a real, verified exhaustion (injector's own test connection
# failed with MySQL's genuine "too many connections" error),
# mysql_global_status_threads_connected stayed flat at baseline (3) the
# entire time -- the exporter's own scrape connection was ALSO
# starved, since our flood's root connections could occupy MySQL's
# reserved SUPER-user slot just as easily as a legitimate admin
# connection.
#
# Fix: a separate, unprivileged user for the flood -- SELECT SLEEP()
# is a built-in function needing no table access, so USAGE (equivalent
# to "no real privileges", just permission to log in) is enough. Root
# stays reserved exclusively for mysqld_exporter's own connection.
kubectl exec -n sock-shop deploy/catalogue-db -- mysql -uroot -pfake_password -e "
CREATE USER IF NOT EXISTS 'floodtest'@'%' IDENTIFIED BY 'floodpass';
GRANT USAGE ON *.* TO 'floodtest'@'%';
FLUSH PRIVILEGES;
"
