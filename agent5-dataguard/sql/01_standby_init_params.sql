-- 01_standby_init_params.sql — minimal init parameters used to start the standby NOMOUNT
-- before RMAN DUPLICATE. Only ASCII parameter=value lines (no SQL*Plus directives) are kept
-- by prepare_standby._stage_pfile_and_start_nomount().
--
-- Bindings:
--   ${DB_NAME}              — db_name (must match primary)
--   ${STANDBY_UNIQUE_NAME}  — standby db_unique_name
--   ${PRIMARY_UNIQUE_NAME}  — primary db_unique_name
--   ${WALLET_ROOT}          — wallet_root parent (e.g., /var/opt/oracle/dbaas_acfs/<db>)

db_name=${DB_NAME}
db_unique_name=${STANDBY_UNIQUE_NAME}
db_block_size=8192
compatible=19.0.0
cluster_database=FALSE
sga_target=8G
pga_aggregate_target=2G
processes=1000
db_files=2000
control_files='+DATA/${STANDBY_UNIQUE_NAME}/CONTROLFILE/control01.ctl','+RECO/${STANDBY_UNIQUE_NAME}/CONTROLFILE/control02.ctl'
db_create_file_dest='+DATA'
db_create_online_log_dest_1='+DATA'
db_create_online_log_dest_2='+RECO'
db_recovery_file_dest='+RECO'
db_recovery_file_dest_size=2T

-- file name conversion so primary datafile/redo paths are translated on standby
db_file_name_convert='${PRIMARY_UNIQUE_NAME}','${STANDBY_UNIQUE_NAME}'
log_file_name_convert='${PRIMARY_UNIQUE_NAME}','${STANDBY_UNIQUE_NAME}'

-- Data Guard parameters (Broker will rewrite some of these post-enable)
log_archive_config='DG_CONFIG=(${PRIMARY_UNIQUE_NAME},${STANDBY_UNIQUE_NAME})'
log_archive_dest_1='LOCATION=USE_DB_RECOVERY_FILE_DEST VALID_FOR=(ALL_LOGFILES,ALL_ROLES) DB_UNIQUE_NAME=${STANDBY_UNIQUE_NAME}'
log_archive_dest_2='SERVICE=${PRIMARY_UNIQUE_NAME} ASYNC NOAFFIRM REOPEN=15 VALID_FOR=(ONLINE_LOGFILES,PRIMARY_ROLE) DB_UNIQUE_NAME=${PRIMARY_UNIQUE_NAME}'
log_archive_dest_state_1=ENABLE
log_archive_dest_state_2=DEFER
log_archive_format='%t_%s_%r.arc'
log_archive_max_processes=8
fal_server='${PRIMARY_UNIQUE_NAME}'
standby_file_management=AUTO
remote_login_passwordfile=EXCLUSIVE
dg_broker_start=TRUE

-- TDE wallet location for standby (must match where prepare_standby copied the wallet to)
wallet_root='${WALLET_ROOT}'
tde_configuration='KEYSTORE_CONFIGURATION=FILE'

-- audit dest (will be created by prepare_standby)
audit_file_dest='/u01/app/oracle/admin/${STANDBY_UNIQUE_NAME}/adump'
audit_trail=DB

-- diagnostic dest
diagnostic_dest='/u01/app/oracle'
