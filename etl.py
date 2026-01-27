import json
import logging
from db import get_connection, create_table_if_not_exists

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def parse_component_list(components):
    """
    Recursively parse a list of components.
    Returns:
      - For top-level form: a dict {Label: Value}
      - For list/table components provided via recursion: a dict {Label: Value}
    """
    result = {}
    
    if not components:
        return result
        
    for comp in components:
        # 1. Get basic info
        c_type = comp.get('component_type') or comp.get('componentType')
        c_id = comp.get('id')
        # Some components use 'name', some use parameters in props. 
        # But usually 'name' or 'label' is available at top level or in props logic.
        # However, the raw data often has "name" at the top level for simple fields.
        c_name = comp.get('name')
        if not c_name and 'props' in comp:
             c_name = comp.get('props', {}).get('label')
             
        # If still no name, use ID as fallback
        if not c_name:
            c_name = c_id
            
        c_value = comp.get('value')
        
        # 2. Handle nested DDBizSuite
        if c_type == 'DDBizSuite':
            if c_value:
                try:
                    # c_value is a JSON string representing a list of components
                    inner_comps = json.loads(c_value)
                    # Recursively parse
                    inner_result = parse_component_list(inner_comps)
                    # Merge into main result
                    result.update(inner_result)
                except Exception as e:
                    logger.warning(f"Failed to parse DDBizSuite value: {e}")
                    result[c_name] = c_value
        
        # 3. Handle TableField (明细)
        elif c_type == 'TableField':
            if c_value:
                try:
                    # c_value is a JSON string representing list of rows
                    # e.g. [{"rowValue": [ {key, label, value}, ... ]}, ...]
                    rows_data = json.loads(c_value)
                    
                    table_list = []
                    for row in rows_data:
                        row_dict = {}
                        row_items = row.get('rowValue', [])
                        for item in row_items:
                            label = item.get('label')
                            val = item.get('value')
                            if label:
                                row_dict[label] = val
                        table_list.append(row_dict)
                    
                    result[c_name] = table_list
                except Exception as e:
                    logger.warning(f"Failed to parse TableField value: {e}")
                    result[c_name] = c_value

        # 4. Handle Standard Fields
        else:
            # Skip some useless fields if value is null/empty, or keep them?
            # User wants readability. Keeping them is safer, but maybe clean 'null' string?
            if c_value == 'null':
                c_value = None
            
            if c_name:
                result[c_name] = c_value

    return result

def process_single_record(record):
    """
    Process one DB record.
    record: dict having 'process_instance_id', 'form_component_values'
    """
    pid = record['process_instance_id']
    raw_val = record['form_component_values']
    
    if not raw_val:
        return None
        
    try:
        if isinstance(raw_val, str):
            form_data = json.loads(raw_val)
        else:
            form_data = raw_val
            
        cleaned_data = parse_component_list(form_data)
        return cleaned_data
    except Exception as e:
        logger.error(f"Error processing record {pid}: {e}")
        return None

def main():
    # Ensure schema is up to date
    create_table_if_not_exists()
    
    conn = get_connection()
    try:
        # Fetch all records
        logger.info("Fetching records...")
        with conn.cursor() as cursor:
            cursor.execute("SELECT process_instance_id, form_component_values FROM process_instance")
            records = cursor.fetchall()
            
        logger.info(f"Found {len(records)} records. Starting ETL...")
        
        updates = []
        for i, rec in enumerate(records):
            cleaned = process_single_record(rec)
            if cleaned:
                cleaned_json = json.dumps(cleaned, ensure_ascii=False)
                updates.append((cleaned_json, rec['process_instance_id']))
                
            if (i + 1) % 100 == 0:
                print(f"Processed {i + 1} records...")

        # Batch Update
        if updates:
            logger.info(f"Updating {len(updates)} records in DB...")
            update_sql = "UPDATE process_instance SET form_values_cleaned = %s WHERE process_instance_id = %s"
            with conn.cursor() as cursor:
                cursor.executemany(update_sql, updates)
            conn.commit()
            logger.info("ETL Completed Successfully.")
        else:
            logger.info("No records to update.")

    except Exception as e:
        logger.critical(f"ETL Failed: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    main()
