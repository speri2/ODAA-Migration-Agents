-- 05_password_file_sync.sql — verify SYS verifier exists and remote_login_passwordfile is EXCLUSIVE.
-- Actual file location is registered with srvctl in the Python step (configure_dg_prep.py).
WHENEVER SQLERROR EXIT FAILURE
SET ECHO ON FEEDBACK ON LINESIZE 200

ALTER SYSTEM SET remote_login_passwordfile='EXCLUSIVE' SCOPE=SPFILE SID='*';

-- Verify SYS user exists in the password file (a Data Guard pre-req)
SELECT username, sysdba, sysoper, sysasm
  FROM v$pwfile_users
 WHERE username = 'SYS';

-- Print the password file location each instance is using
SELECT inst_id, value FROM gv$parameter WHERE name = 'remote_login_passwordfile';
SELECT inst_id, value FROM gv$parameter WHERE name = 'spfile';
EXIT SUCCESS;
