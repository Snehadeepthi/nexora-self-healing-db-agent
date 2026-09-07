-- mysql_procedures.sql
--
-- Two stored procedures backing MySQL's Tier 2/3 batch-termination actions
-- (mysql_terminate_idle_in_transaction / mysql_reset_all_connections in
-- tools.yaml). MySQL's KILL statement has no subquery/WHERE-clause form
-- the way Postgres's `pg_terminate_backend(pid) FROM pg_stat_activity
-- WHERE ...` does -- there is no "KILL ALL matching X" single statement --
-- so batch termination here loops a cursor and KILLs each matching
-- PROCESSLIST_ID individually. Run once against the instance (app_user
-- already has CREATE ROUTINE/EXECUTE -- confirmed via SHOW GRANTS during
-- setup); idempotent via DROP PROCEDURE IF EXISTS.
DROP PROCEDURE IF EXISTS sp_mysql_terminate_idle_in_transaction;
DROP PROCEDURE IF EXISTS sp_mysql_reset_all_connections;

DELIMITER $$

CREATE PROCEDURE sp_mysql_terminate_idle_in_transaction(IN idle_seconds INT)
BEGIN
  DECLARE done INT DEFAULT FALSE;
  DECLARE tid BIGINT;
  DECLARE cur CURSOR FOR
    SELECT th.PROCESSLIST_ID
    FROM performance_schema.events_transactions_current etc
    JOIN performance_schema.threads th ON th.THREAD_ID = etc.THREAD_ID
    WHERE etc.STATE = 'ACTIVE'
      AND th.PROCESSLIST_COMMAND = 'Sleep'
      AND th.PROCESSLIST_TIME > idle_seconds;
  DECLARE CONTINUE HANDLER FOR NOT FOUND SET done = TRUE;

  OPEN cur;
  read_loop: LOOP
    FETCH cur INTO tid;
    IF done THEN
      LEAVE read_loop;
    END IF;
    KILL tid;
  END LOOP;
  CLOSE cur;
END$$

CREATE PROCEDURE sp_mysql_reset_all_connections()
BEGIN
  DECLARE done INT DEFAULT FALSE;
  DECLARE tid BIGINT;
  DECLARE cur CURSOR FOR
    SELECT ID FROM information_schema.processlist
    WHERE ID <> CONNECTION_ID()
      AND COMMAND <> 'Daemon';
  DECLARE CONTINUE HANDLER FOR NOT FOUND SET done = TRUE;

  OPEN cur;
  read_loop: LOOP
    FETCH cur INTO tid;
    IF done THEN
      LEAVE read_loop;
    END IF;
    KILL tid;
  END LOOP;
  CLOSE cur;
END$$

DELIMITER ;
