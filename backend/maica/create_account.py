import sqlite3
import bcrypt
import argparse
import logging
import os
from datetime import datetime

def setup_logging(log_dir=None, custom_log_path=None, db_path='forum_flarum_db.db'):
    # Determine log directory
    if custom_log_path:
        log_dir = custom_log_path
    elif log_dir is None:
        # Use the directory of the database file as the default log directory
        log_dir = os.path.dirname(db_path) or '.'

    # Create log directory if it doesn't exist
    os.makedirs(log_dir, exist_ok=True)
    
    # Generate a log filename with timestamp
    log_filename = os.path.join(log_dir, f'user_creation_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log')
    
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_filename),
            logging.StreamHandler()
        ]
    )
    return log_filename

def create_user(db_path, username, email, password, log_dir=None):
    # Setup logging
    log_filename = setup_logging(custom_log_path=log_dir, db_path=db_path)
    logging.info(f"Logging initialized. Log file: {log_filename}")
    
    logging.info(f"Attempting to create user: {username} (email: {email})")
    
    try:
        # Validate input data
        if not all([username, email, password]):
            logging.error("Invalid input: Missing required fields")
            return False
        
        # Hash the password using bcrypt
        logging.info("Hashing password...")
        hashed_password = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())

        # Connect to the SQLite database
        logging.info(f"Connecting to database: {db_path}")
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        try:
            # Insert the new user into the users table
            logging.info("Executing user insertion query...")
            cursor.execute('''
                INSERT INTO `users` 
                (`username`, `email`, `password`, `is_email_confirmed`) 
                VALUES (?, ?, ?, ?)
            ''', (username, email, hashed_password.decode('utf-8'), 1))
            
            # Commit the transaction
            conn.commit()
            logging.info(f"User {username} created successfully.")
            return True
        except sqlite3.IntegrityError as e:
            logging.error(f"Integrity Error: Likely duplicate username or email - {e}")
        except sqlite3.Error as e:
            logging.error(f"Database Error during user creation: {e}")
        finally:
            # Close the database connection
            conn.close()
            logging.info("Database connection closed.")
    except Exception as e:
        logging.error(f"Unexpected error during user creation: {e}")
    
    return False

def main():
    # Set up argument parsing
    parser = argparse.ArgumentParser(description='Create a new user in the Flarum SQLite database')
    parser.add_argument('--db', default='forum_flarum_db.db', help='Path to the SQLite database')
    parser.add_argument('--username', required=True, help='Username for the new user')
    parser.add_argument('--email', required=True, help='Email for the new user')
    parser.add_argument('--password', required=True, help='Password for the new user')
    parser.add_argument('--log-dir', default=None, help='Custom directory for log files')

    # Parse arguments
    args = parser.parse_args()

    # Create the user
    result = create_user(args.db, args.username, args.email, args.password, log_dir=args.log_dir)
    
    # Exit with appropriate status code
    exit(0 if result else 1)

if __name__ == '__main__':
    main()