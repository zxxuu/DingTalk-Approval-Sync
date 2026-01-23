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
    """Create the process_instance table if it doesn't exist."""
    create_table_sql = """
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
        `update_time` DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT 'Last Sync Time',
        PRIMARY KEY (`process_instance_id`),
        KEY `idx_create_time` (`create_time`),
        KEY `idx_process_code` (`process_code`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='DingTalk Process Instances';
    """
    
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(create_table_sql)
            
            # Check for missing columns (simple schema migration for existing tables if any)
            # For a brand new table, this isn't strictly necessary but good for resilience if re-running
            # Logic omitted for brevity as we are likely creating new or replacing.
                
        conn.commit()
        logger.info("Table `process_instance` checked/created successfully.")
    except Exception as e:
        logger.error(f"Error creating/updating table: {e}")
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

    upsert_sql = """
    INSERT INTO `process_instance` (
        `process_instance_id`, `title`, `create_time`, `finish_time`,
        `originator_userid`, `originator_dept_id`, `status`, `result`,
        `business_id`, `process_code`, `form_component_values`
    ) VALUES (
        %(process_instance_id)s, %(title)s, %(create_time)s, %(finish_time)s,
        %(originator_userid)s, %(originator_dept_id)s, %(status)s, %(result)s,
        %(business_id)s, %(process_code)s, %(form_component_values)s
    ) AS new
    ON DUPLICATE KEY UPDATE
        `title` = new.title,
        `finish_time` = new.finish_time,
        `status` = new.status,
        `result` = new.result,
        `form_component_values` = new.form_component_values,
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

