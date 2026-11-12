"""
Loads all CSVs from a given folder into PostgreSQL.
One table per CSV, no primary/foreign keys.
"""
import os
import pandas as pd
from sqlalchemy import create_engine
from dotenv import load_dotenv

load_dotenv()

def load_all_csvs(csv_folder: str, engine):
    """Iterate over all .csv files in the folder and load each to a table."""
    for filename in os.listdir(csv_folder):
        if not filename.endswith('.csv'):
            continue
        table_name = os.path.splitext(filename)[0]
        file_path = os.path.join(csv_folder, filename)
        print(f"Loading {filename} -> table '{table_name}'")
        df = pd.read_csv(file_path)
        # Clean column names for PostgreSQL
        df.columns = [c.strip().lower().replace(' ', '_') for c in df.columns]
        df.to_sql(table_name, engine, if_exists='replace', index=False)

if __name__ == "__main__":
   db_url = f"postgresql+psycopg2://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}@" \
         f"{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"
   engine = create_engine(db_url)
    # Point this to your extracted Olist CSV folder
   load_all_csvs("./olist_csvs", engine)
   print("All CSVs loaded.")