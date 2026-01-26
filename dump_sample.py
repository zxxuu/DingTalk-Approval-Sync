import json
from db import get_connection

def dump_sample():
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT form_component_values FROM process_instance WHERE form_component_values IS NOT NULL LIMIT 1")
            result = cursor.fetchone()
            if result:
                data = result['form_component_values']
                if isinstance(data, str):
                    data = json.loads(data)
                
                with open('sample_data.json', 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                print("Dumped to sample_data.json")
            else:
                print("No data found")
    finally:
        conn.close()

if __name__ == "__main__":
    dump_sample()
