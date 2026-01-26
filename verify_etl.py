import json
from db import get_connection

def verify_etl():
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            # Fetch one record that has form_values_cleaned populated
            cursor.execute("SELECT form_values_cleaned FROM process_instance WHERE form_values_cleaned IS NOT NULL LIMIT 1")
            result = cursor.fetchone()
            if result:
                data = result['form_values_cleaned']
                if isinstance(data, str):
                    data = json.loads(data)
                
                print(json.dumps(data, indent=2, ensure_ascii=False))
            else:
                print("No cleaned data found.")
    finally:
        conn.close()

if __name__ == "__main__":
    verify_etl()
