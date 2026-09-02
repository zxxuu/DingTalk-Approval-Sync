import os
import pymysql
from dotenv import load_dotenv
import logging
import json

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def get_connection():
    """Create and return a database connection."""
    try:
        connection = pymysql.connect(
            host=os.getenv('DB_HOST', 'localhost'),
            port=int(os.getenv('DB_PORT', 3306)),
            user=os.getenv('DB_USER', 'root'),
            password=os.getenv('DB_PASSWORD', ''),
            database=os.getenv('DB_NAME', '工程信息'),
            charset='utf8mb4',
            cursorclass=pymysql.cursors.DictCursor
        )
        return connection
    except Exception as e:
        logger.error(f"Error connecting to database: {e}")
        raise

def create_table_if_not_exists():
    """Create the process_instance and dingtalk_user tables if they don't exist."""
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            # 1. Create process_instance table
            create_pi_sql = """
            CREATE TABLE IF NOT EXISTS `process_instance` (
                `process_instance_id` VARCHAR(64) NOT NULL COMMENT 'Process Instance ID',
                `title` VARCHAR(255) COMMENT 'Approval Title',
                `create_time` DATETIME COMMENT 'Creation Time',
                `finish_time` DATETIME COMMENT 'Finish Time',
                `originator_userid` VARCHAR(64) COMMENT 'Originator User ID',
                `originator_dept_id` VARCHAR(64) COMMENT 'Originator Dept ID',
                `status` VARCHAR(32) COMMENT 'Status: NEW, RUNNING, COMPLETED, TERMINATED',
                `result` VARCHAR(32) COMMENT 'Result: agree, refuse, etc.',
                `business_id` VARCHAR(128) COMMENT 'Business ID',
                `process_code` VARCHAR(64) COMMENT 'Process Code (Template ID)',
                `form_component_values` JSON COMMENT 'Full Form Data',
                `originator_name` VARCHAR(64) COMMENT 'Originator Name',
                `current_approvers` VARCHAR(512) COMMENT 'Current Approvers Names',
                `update_time` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT 'Last Sync Time',
                `tasks` JSON COMMENT 'Raw Tasks List',
                `form_values_cleaned` JSON COMMENT 'Cleaned Form Data',
                PRIMARY KEY (`process_instance_id`),
                KEY `idx_create_time` (`create_time`),
                KEY `idx_process_code` (`process_code`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='DingTalk Process Instances';
            """
            cursor.execute(create_pi_sql)

            # Check for new columns in process_instance (for migration)
            cursor.execute("SHOW COLUMNS FROM `process_instance` LIKE 'originator_name'")
            if not cursor.fetchone():
                logger.info("Adding column `originator_name` to process_instance...")
                cursor.execute("ALTER TABLE `process_instance` ADD COLUMN `originator_name` VARCHAR(64) COMMENT 'Originator Name' AFTER `form_component_values`")

            cursor.execute("SHOW COLUMNS FROM `process_instance` LIKE 'current_approvers'")
            if not cursor.fetchone():
                logger.info("Adding column `current_approvers` to process_instance...")
                cursor.execute("ALTER TABLE `process_instance` ADD COLUMN `current_approvers` VARCHAR(512) COMMENT 'Current Approvers Names' AFTER `originator_name`")

            cursor.execute("SHOW COLUMNS FROM `process_instance` LIKE 'tasks'")
            if not cursor.fetchone():
                logger.info("Adding column `tasks` to process_instance...")
                cursor.execute("ALTER TABLE `process_instance` ADD COLUMN `tasks` JSON COMMENT 'Raw Tasks List' AFTER `current_approvers`")

            cursor.execute("SHOW COLUMNS FROM `process_instance` LIKE 'form_values_cleaned'")
            if not cursor.fetchone():
                logger.info("Adding column `form_values_cleaned` to process_instance...")
                cursor.execute("ALTER TABLE `process_instance` ADD COLUMN `form_values_cleaned` JSON COMMENT 'Cleaned Form Data' AFTER `tasks`")

            # 2. Create dingtalk_user table
            create_user_sql = """
            CREATE TABLE IF NOT EXISTS `dingtalk_user` (
                `userid` VARCHAR(64) NOT NULL COMMENT 'User ID',
                `name` VARCHAR(64) COMMENT 'User Name',
                `dept_ids` JSON COMMENT 'Department IDs',
                `update_time` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT 'Last Update Time',
                PRIMARY KEY (`userid`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='DingTalk Users Cache';
            """
            cursor.execute(create_user_sql)

            # 3. Create process_asset table (图片/附件转储资产表)
            create_asset_sql = """
            CREATE TABLE IF NOT EXISTS `process_asset` (
                `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                `asset_type` VARCHAR(16) NOT NULL COMMENT 'image | attachment',
                `process_instance_id` VARCHAR(64) NOT NULL COMMENT 'Process Instance ID',
                `asset_ref` VARCHAR(255) NOT NULL COMMENT 'Dedup key: image=source url, attachment=fileId',
                `component_name` VARCHAR(255) DEFAULT NULL COMMENT 'Form component label',
                `file_name` VARCHAR(512) DEFAULT NULL COMMENT 'Original file name (attachment only)',
                `source_url` VARCHAR(1024) DEFAULT NULL COMMENT 'DingTalk image direct url',
                `file_id` VARCHAR(64) DEFAULT NULL COMMENT 'DingTalk attachment fileId',
                `space_id` VARCHAR(64) DEFAULT NULL COMMENT 'DingTalk cspace spaceId',
                `bucket` VARCHAR(64) DEFAULT NULL COMMENT 'MinIO bucket',
                `object_key` VARCHAR(255) DEFAULT NULL COMMENT 'MinIO object key',
                `sha256` CHAR(64) DEFAULT NULL COMMENT 'Content hash for dedup and integrity',
                `size_bytes` INT UNSIGNED DEFAULT NULL COMMENT 'Actual bytes stored',
                `declared_size` INT UNSIGNED DEFAULT NULL COMMENT 'Size declared by DingTalk (attachment)',
                `content_type` VARCHAR(64) DEFAULT NULL COMMENT 'MIME type',
                `status` VARCHAR(16) NOT NULL DEFAULT 'PENDING' COMMENT 'PENDING|SUCCESS|FAILED|SKIPPED',
                `error_message` VARCHAR(512) DEFAULT NULL COMMENT 'Last failure reason',
                `retry_count` TINYINT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'Retry attempts',
                `create_time` DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT 'First seen',
                `update_time` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT 'Last change',
                PRIMARY KEY (`id`),
                UNIQUE KEY `uk_asset` (`process_instance_id`, `asset_type`, `asset_ref`),
                KEY `idx_status` (`status`),
                KEY `idx_object` (`object_key`),
                KEY `idx_sha256` (`sha256`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='DingTalk process image & attachment assets';
            """
            cursor.execute(create_asset_sql)

            # 4. Create process_operation_record table (操作记录 + 评论)
            #    ADD_REMARK 即审批评论，其余为发起/审批/抄送/终止等流转记录
            create_op_sql = """
            CREATE TABLE IF NOT EXISTS `process_operation_record` (
                `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
                `process_instance_id` VARCHAR(64) NOT NULL COMMENT 'Process Instance ID',
                `seq` SMALLINT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'Index within the instance',
                `operation_type` VARCHAR(48) NOT NULL COMMENT 'START_PROCESS_INSTANCE|EXECUTE_TASK_NORMAL|ADD_REMARK|PROCESS_CC|TERMINATE_PROCESS_INSTANCE',
                `operation_result` VARCHAR(32) DEFAULT NULL COMMENT 'NONE|AGREE|REFUSE',
                `userid` VARCHAR(64) DEFAULT NULL COMMENT 'Operator user id',
                `user_name` VARCHAR(64) DEFAULT NULL COMMENT 'Operator name, joined from dingtalk_user',
                `remark` TEXT COMMENT 'Approval comment / remark',
                `operation_time` DATETIME DEFAULT NULL COMMENT 'Operation timestamp from DingTalk',
                `create_time` DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT 'First seen',
                PRIMARY KEY (`id`),
                UNIQUE KEY `uk_record` (`process_instance_id`, `seq`),
                KEY `idx_type` (`operation_type`),
                KEY `idx_time` (`operation_time`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='DingTalk process operation records and comments';
            """
            cursor.execute(create_op_sql)

            # 5. Incremental columns on process_instance (评论增量同步的指纹)
            for col, ddl in (
                ('op_record_count', "SMALLINT UNSIGNED DEFAULT NULL COMMENT 'Operation record count fingerprint'"),
                ('last_op_time', "DATETIME DEFAULT NULL COMMENT 'Last operation time fingerprint'"),
                ('asset_synced', "TINYINT(1) NOT NULL DEFAULT 0 COMMENT 'Whether assets have been enqueued'"),
            ):
                cursor.execute("SHOW COLUMNS FROM `process_instance` LIKE %s", (col,))
                if not cursor.fetchone():
                    logger.info(f"Adding column `{col}` to process_instance...")
                    cursor.execute(f"ALTER TABLE `process_instance` ADD COLUMN `{col}` {ddl}")

        conn.commit()
        logger.info("Tables checked/created successfully.")
    except Exception as e:
        logger.error(f"Error creating/updating tables: {e}")
        raise
    finally:
        conn.close()

def upsert_process_instance(data):
    """
    Upsert a single process instance record.
    data: Dictionary containing record fields.
    """
    if not data:
        return

    # Ensure JSON fields are serialized if passed as dict/list
    if isinstance(data.get('form_component_values'), (dict, list)):
        data['form_component_values'] = json.dumps(data['form_component_values'], ensure_ascii=False)
    
    if isinstance(data.get('tasks'), (dict, list)):
        data['tasks'] = json.dumps(data['tasks'], ensure_ascii=False)
        
    if isinstance(data.get('form_values_cleaned'), (dict, list)):
        data['form_values_cleaned'] = json.dumps(data.get('form_values_cleaned'), ensure_ascii=False)

    upsert_sql = """
    INSERT INTO `process_instance` (
        `process_instance_id`, `title`, `create_time`, `finish_time`,
        `originator_userid`, `originator_dept_id`, `status`, `result`,
        `business_id`, `process_code`, `form_component_values`,
        `originator_name`, `current_approvers`, `tasks`, `form_values_cleaned`
    ) VALUES (
        %(process_instance_id)s, %(title)s, %(create_time)s, %(finish_time)s,
        %(originator_userid)s, %(originator_dept_id)s, %(status)s, %(result)s,
        %(business_id)s, %(process_code)s, %(form_component_values)s,
        %(originator_name)s, %(current_approvers)s, %(tasks)s, %(form_values_cleaned)s
    ) AS new
    ON DUPLICATE KEY UPDATE
        `title` = new.title,
        `finish_time` = new.finish_time,
        `status` = new.status,
        `result` = new.result,
        `form_component_values` = new.form_component_values,
        `originator_name` = new.originator_name,
        `current_approvers` = new.current_approvers,
        `tasks` = new.tasks,
        `form_values_cleaned` = new.form_values_cleaned,
        `update_time` = NOW();
    """

    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(upsert_sql, data)
        conn.commit()
        # logger.info(f"Successfully upserted process instance {data.get('process_instance_id')}")
    except Exception as e:
        logger.error(f"Error upserting process instance {data.get('process_instance_id')}: {e}")
        raise
    finally:
        conn.close()

def upsert_dingtalk_users(users):
    """
    Batch upsert dingtalk users.
    users: List of dicts {'userid': '...', 'name': '...'}
    """
    if not users:
        return

    upsert_sql = """
    INSERT INTO `dingtalk_user` (`userid`, `name`)
    VALUES (%(userid)s, %(name)s)
    AS new
    ON DUPLICATE KEY UPDATE
        `name` = new.name,
        `update_time` = NOW();
    """
    
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.executemany(upsert_sql, users)
        conn.commit()
        logger.info(f"Successfully upserted {len(users)} users.")
    except Exception as e:
        logger.error(f"Error upserting users: {e}")
        raise
    finally:
        conn.close()

def get_user_name_from_db(userid):
    """
    Get user name from cache table.
    """
    if not userid:
        return None
        
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT name FROM `dingtalk_user` WHERE userid = %s", (userid,))
            result = cursor.fetchone()
            if result:
                return result['name']
    except Exception as e:
        logger.error(f"Error fetching user name: {e}")
    finally:
        conn.close()
    return None

def get_instance_status(process_instance_id):
    """
    Check if an instance exists and return its status.
    Returns: status string (e.g. 'COMPLETED', 'RUNNING') or None if not found.
    """
    if not process_instance_id:
        return None
        
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT status FROM `process_instance` WHERE process_instance_id = %s", (process_instance_id,))
            result = cursor.fetchone()
            if result:
                return result['status']
    except Exception as e:
        logger.error(f"Error checking instance status: {e}")
    finally:
        conn.close()
    return None

# --- Process Asset (图片/附件转储) ---

def insert_assets_pending(assets):
    """
    Batch register assets as PENDING. Existing rows are left untouched.
    assets: list of dicts with keys asset_type, process_instance_id, asset_ref,
            component_name, file_name, source_url, file_id, space_id, declared_size
    Returns: number of newly inserted rows.
    """
    if not assets:
        return 0

    sql = """
    INSERT IGNORE INTO `process_asset` (
        `asset_type`, `process_instance_id`, `asset_ref`, `component_name`,
        `file_name`, `source_url`, `file_id`, `space_id`, `declared_size`
    ) VALUES (
        %(asset_type)s, %(process_instance_id)s, %(asset_ref)s, %(component_name)s,
        %(file_name)s, %(source_url)s, %(file_id)s, %(space_id)s, %(declared_size)s
    )
    """
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            affected = cursor.executemany(sql, assets)
        conn.commit()
        return affected
    except Exception as e:
        logger.error(f"Error inserting pending assets: {e}")
        raise
    finally:
        conn.close()

def fetch_pending_assets(asset_type=None, limit=100):
    """
    Fetch assets waiting to be transferred, oldest first, retry_count capped at 3.
    """
    sql = """
    SELECT * FROM `process_asset`
    WHERE `status` = 'PENDING' AND `retry_count` < 3
    """
    params = []
    if asset_type:
        sql += " AND `asset_type` = %s"
        params.append(asset_type)
    sql += " ORDER BY `id` ASC LIMIT %s"
    params.append(limit)

    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()
    except Exception as e:
        logger.error(f"Error fetching pending assets: {e}")
        raise
    finally:
        conn.close()

def mark_asset_result(asset_id, status, bucket=None, object_key=None, sha256=None,
                      size_bytes=None, content_type=None, error_message=None):
    """
    Update an asset row after a transfer attempt.
    status: SUCCESS | SKIPPED | FAILED
    """
    sql = """
    UPDATE `process_asset`
    SET `status` = %s,
        `bucket` = COALESCE(%s, `bucket`),
        `object_key` = COALESCE(%s, `object_key`),
        `sha256` = COALESCE(%s, `sha256`),
        `size_bytes` = COALESCE(%s, `size_bytes`),
        `content_type` = COALESCE(%s, `content_type`),
        `error_message` = %s,
        `retry_count` = `retry_count` + CASE WHEN %s = 'FAILED' THEN 1 ELSE 0 END
    WHERE `id` = %s
    """
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, (status, bucket, object_key, sha256, size_bytes,
                                 content_type, error_message, status, asset_id))
        conn.commit()
    except Exception as e:
        logger.error(f"Error updating asset {asset_id}: {e}")
        raise
    finally:
        conn.close()

def fetch_assets_by_instance(process_instance_id):
    """Return all asset rows for one instance, in registration order."""
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM `process_asset` WHERE `process_instance_id` = %s ORDER BY `id` ASC",
                (process_instance_id,))
            return cursor.fetchall()
    except Exception as e:
        logger.error(f"Error fetching assets for {process_instance_id}: {e}")
        raise
    finally:
        conn.close()

def count_assets_by_status():
    """Return a dict like {'PENDING': 12, 'SUCCESS': 690, ...}"""
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT `status`, COUNT(*) c FROM `process_asset` GROUP BY `status`")
            return {r['status']: r['c'] for r in cursor.fetchall()}
    except Exception as e:
        logger.error(f"Error counting assets: {e}")
        raise
    finally:
        conn.close()

# --- Process Operation Record (操作记录 + 评论) ---

def upsert_operation_records(process_instance_id, records):
    """
    Upsert operation records for one instance.
    records: list of dicts with seq, operation_type, operation_result,
             userid, user_name, remark, operation_time
    """
    if not records:
        return 0

    sql = """
    INSERT INTO `process_operation_record` (
        `process_instance_id`, `seq`, `operation_type`, `operation_result`,
        `userid`, `user_name`, `remark`, `operation_time`
    ) VALUES (
        %(process_instance_id)s, %(seq)s, %(operation_type)s, %(operation_result)s,
        %(userid)s, %(user_name)s, %(remark)s, %(operation_time)s
    ) AS new
    ON DUPLICATE KEY UPDATE
        `operation_type` = new.operation_type,
        `operation_result` = new.operation_result,
        `userid` = new.userid,
        `user_name` = new.user_name,
        `remark` = new.remark,
        `operation_time` = new.operation_time
    """
    for r in records:
        r['process_instance_id'] = process_instance_id

    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            affected = cursor.executemany(sql, records)
        conn.commit()
        return affected
    except Exception as e:
        logger.error(f"Error upserting operation records for {process_instance_id}: {e}")
        raise
    finally:
        conn.close()

def update_instance_fingerprint(process_instance_id, op_record_count=None,
                                last_op_time=None, asset_synced=None):
    """
    Store the operation-record fingerprint used by incremental refresh.
    Only the fields explicitly provided are written; None means "leave unchanged".
    """
    sets, params = [], []
    if op_record_count is not None:
        sets.append("`op_record_count` = %s")
        params.append(op_record_count)
    if last_op_time is not None:
        sets.append("`last_op_time` = %s")
        params.append(last_op_time)
    if asset_synced is not None:
        sets.append("`asset_synced` = %s")
        params.append(asset_synced)
    if not sets:
        return

    sql = "UPDATE `process_instance` SET " + ", ".join(sets) + " WHERE `process_instance_id` = %s"
    params.append(process_instance_id)

    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
        conn.commit()
    except Exception as e:
        logger.error(f"Error updating fingerprint for {process_instance_id}: {e}")
        raise
    finally:
        conn.close()

def get_instance_ids_for_refresh(limit=None, since_days=None):
    """
    Return process_instance_id list, newest first.
    since_days: only instances created within the last N days.
    """
    sql = "SELECT `process_instance_id` FROM `process_instance`"
    params = []
    if since_days:
        sql += " WHERE `create_time` >= DATE_SUB(NOW(), INTERVAL %s DAY)"
        params.append(since_days)
    sql += " ORDER BY `create_time` DESC"
    if limit:
        sql += " LIMIT %s"
        params.append(limit)

    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            return [r['process_instance_id'] for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Error listing instance ids: {e}")
        raise
    finally:
        conn.close()

def get_records_without_assets(limit=200):
    """
    Return instances whose form data has never been scanned for assets.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT `process_instance_id` FROM `process_instance` "
                "WHERE `asset_synced` = 0 ORDER BY `create_time` DESC LIMIT %s",
                (limit,))
            return [r['process_instance_id'] for r in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Error listing unsynced instances: {e}")
        raise
    finally:
        conn.close()
