import json
from db import get_connection

def inspect_one():
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT form_component_values FROM process_instance LIMIT 1")
            result = cursor.fetchone()
            if result and result.get('form_component_values'):
                # It might be returned as string or list/dict depending on the driver and create_table
                data = result['form_component_values']
                if isinstance(data, str):
                    data = json.loads(data)
                
                print(json.dumps(data, indent=2, ensure_ascii=False))
            else:
                print("No data found or empty field.")
    finally:
        conn.close()

if __name__ == "__main__":
    inspect_one()
