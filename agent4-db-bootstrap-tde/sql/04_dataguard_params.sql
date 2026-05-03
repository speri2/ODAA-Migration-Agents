-- 04_dataguard_params.sql — set the init parameters Data Guard expects on the primary.
--
-- Bindings (caller-provided):
--   ${DB_NAME}              — db_name (e.g., MYCDB)
--   ${DB_UNIQUE_NAME}       — primary db_unique_name (e.g., MYCDB_PRI)
--   ${STANDBY_UNIQUE_NAME}  — standby db_unique_name (e.g., MYCDB_STBY)
WHENEVER SQLERROR EXIT FAILURE
SET ECHO ON FEEDBACK ON LINESIZE 200

ALTER SYSTEM SET db_unique_name='${DB_UNIQUE_NAME}' SCOPE=SPFILE SID='*';

ALTER SYSTEM SET log_archive_config='DG_CONFIG=(${DB_UNIQUE_NAME},${STANDBY_UNIQUE_NAME})' SCOPE=BOTH SID='*';

-- LOG_ARCHIVE_DEST_1 — local FRA (managed by FRA when not explicit; but be explicit for DG)
ALTER SYSTEM SET log_archive_dest_1=
  'LOCATION=USE_DB_RECOVERY_FILE_DEST VALID_FOR=(ALL_LOGFILES,ALL_ROLES) DB_UNIQUE_NAME=${DB_UNIQUE_NAME}'
  SCOPE=BOTH SID='*';

-- LOG_ARCHIVE_DEST_2 — remote, redo to standby. Broker will rewrite when enabled,
-- but Broker config requires this to be set initially.
ALTER SYSTEM SET log_archive_dest_2=
  'SERVICE=${STANDBY_UNIQUE_NAME} ASYNC NOAFFIRM REOPEN=15 VALID_FOR=(ONLINE_LOGFILES,PRIMARY_ROLE) DB_UNIQUE_NAME=${STANDBY_UNIQUE_NAME}'
  SCOPE=BOTH SID='*';

ALTER SYSTEM SET log_archive_dest_state_1='ENABLE' SCOPE=BOTH SID='*';
ALTER SYSTEM SET log_archive_dest_state_2='DEFER'  SCOPE=BOTH SID='*';  -- enabled by Broker post-cutover

ALTER SYSTEM SET log_archive_format='%t_%s_%r.arc' SCOPE=SPFILE SID='*';
ALTER SYSTEM SET log_archive_max_processes=8 SCOPE=BOTH SID='*';
ALTER SYSTEM SET log_archive_min_succeed_dest=1 SCOPE=BOTH SID='*';

-- Data Guard / role transition parameters
ALTER SYSTEM SET fal_server='${STANDBY_UNIQUE_NAME}' SCOPE=BOTH SID='*';
ALTER SYSTEM SET standby_file_management='AUTO' SCOPE=BOTH SID='*';
ALTER SYSTEM SET db_file_name_convert='${STANDBY_UNIQUE_NAME}','${DB_UNIQUE_NAME}' SCOPE=SPFILE SID='*';
ALTER SYSTEM SET log_file_name_convert='${STANDBY_UNIQUE_NAME}','${DB_UNIQUE_NAME}' SCOPE=SPFILE SID='*';

-- Broker can manage these — but pre-set is safe and reproducible
ALTER SYSTEM SET dg_broker_start=TRUE SCOPE=BOTH SID='*';

-- FRA sizing — Data Guard archive retention requires headroom. Caller can override post-deploy.
-- (Leave value as configured by dbaascli unless under-sized.)
SELECT name, value FROM v$parameter
 WHERE name IN (
   'db_unique_name','log_archive_config','log_archive_dest_1','log_archive_dest_2',
   'log_archive_dest_state_1','log_archive_dest_state_2',
   'fal_server','standby_file_management','dg_broker_start',
   'db_recovery_file_dest','db_recovery_file_dest_size'
 ) ORDER BY name;
EXIT SUCCESS;
