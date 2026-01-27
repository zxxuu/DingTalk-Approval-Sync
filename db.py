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
