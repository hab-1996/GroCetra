import sqlite3
from pathlib import Path


USER_FIELDS = (
    "customer_id",
    "first_name",
    "last_name",
    "gender",
    "date_of_birth",
    "country",
    "city",
    "address",
    "email",
    "username",
    "password_hash",
    "points",
    "image",
)


def connect_database(database_path):
    database_path = Path(database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database(database_path):
    with connect_database(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                customer_id INTEGER PRIMARY KEY,
                first_name TEXT NOT NULL,
                last_name TEXT NOT NULL,
                gender TEXT NOT NULL,
                date_of_birth TEXT NOT NULL,
                country TEXT NOT NULL,
                city TEXT NOT NULL,
                address TEXT NOT NULL,
                email TEXT NOT NULL COLLATE NOCASE UNIQUE,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT NOT NULL,
                points INTEGER NOT NULL DEFAULT 0,
                image TEXT
            );

            CREATE TABLE IF NOT EXISTS cart_items (
                owner_key TEXT NOT NULL,
                item_code INTEGER NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                PRIMARY KEY (owner_key, item_code)
            );

            CREATE TABLE IF NOT EXISTS purchase_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                item_code INTEGER NOT NULL,
                item_name TEXT NOT NULL,
                category TEXT NOT NULL,
                uom TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                unit_price REAL NOT NULL,
                total_price REAL NOT NULL,
                purchased_at TEXT NOT NULL,
                FOREIGN KEY (customer_id) REFERENCES users(customer_id)
            );
            """
        )


def create_user(database_path, user):
    with connect_database(database_path) as connection:
        next_id = connection.execute(
            "SELECT COALESCE(MAX(customer_id), 48561000) + 1 FROM users"
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO users (
                customer_id, first_name, last_name, gender, date_of_birth,
                country, city, address, email, username, password_hash,
                points, image
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                next_id,
                user["first_name"],
                user["last_name"],
                user["gender"],
                user["date_of_birth"],
                user["country"],
                user["city"],
                user["address"],
                user["email"],
                user["username"],
                user["password_hash"],
                user.get("points", 0),
                user.get("image"),
            ),
        )
    return next_id


def get_user_by_username(database_path, username):
    with connect_database(database_path) as connection:
        return connection.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()


def get_user_by_id(database_path, customer_id):
    with connect_database(database_path) as connection:
        return connection.execute(
            "SELECT * FROM users WHERE customer_id = ?", (customer_id,)
        ).fetchone()


def update_user_image(database_path, customer_id, image):
    with connect_database(database_path) as connection:
        connection.execute(
            "UPDATE users SET image = ? WHERE customer_id = ?", (image, customer_id)
        )


def user_exists(database_path, email=None, username=None):
    clauses = []
    values = []
    if email:
        clauses.append("email = ? COLLATE NOCASE")
        values.append(email)
    if username:
        clauses.append("username = ? COLLATE NOCASE")
        values.append(username)
    if not clauses:
        return False

    with connect_database(database_path) as connection:
        query = f"SELECT 1 FROM users WHERE {' OR '.join(clauses)} LIMIT 1"
        return connection.execute(query, values).fetchone() is not None


def add_cart_item(database_path, owner_key, item_code):
    with connect_database(database_path) as connection:
        connection.execute(
            """
            INSERT INTO cart_items (owner_key, item_code, quantity)
            VALUES (?, ?, 1)
            ON CONFLICT(owner_key, item_code)
            DO UPDATE SET quantity = quantity + 1
            """,
            (owner_key, item_code),
        )


def get_cart_items(database_path, owner_key):
    with connect_database(database_path) as connection:
        return connection.execute(
            "SELECT item_code, quantity FROM cart_items WHERE owner_key = ? ORDER BY item_code",
            (owner_key,),
        ).fetchall()


def update_cart_quantity(database_path, owner_key, item_code, change):
    with connect_database(database_path) as connection:
        row = connection.execute(
            "SELECT quantity FROM cart_items WHERE owner_key = ? AND item_code = ?",
            (owner_key, item_code),
        ).fetchone()
        if row is None:
            return
        new_quantity = row["quantity"] + change
        if new_quantity <= 0:
            connection.execute(
                "DELETE FROM cart_items WHERE owner_key = ? AND item_code = ?",
                (owner_key, item_code),
            )
        else:
            connection.execute(
                "UPDATE cart_items SET quantity = ? WHERE owner_key = ? AND item_code = ?",
                (new_quantity, owner_key, item_code),
            )


def remove_cart_item(database_path, owner_key, item_code):
    with connect_database(database_path) as connection:
        connection.execute(
            "DELETE FROM cart_items WHERE owner_key = ? AND item_code = ?",
            (owner_key, item_code),
        )


def clear_cart(database_path, owner_key):
    with connect_database(database_path) as connection:
        connection.execute("DELETE FROM cart_items WHERE owner_key = ?", (owner_key,))


def save_purchase(database_path, customer_id, owner_key, entries, points):
    with connect_database(database_path) as connection:
        connection.execute(
            "UPDATE users SET points = points + ? WHERE customer_id = ?",
            (points, customer_id),
        )
        connection.executemany(
            """
            INSERT INTO purchase_history (
                customer_id, item_code, item_name, category, uom, quantity,
                unit_price, total_price, purchased_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    customer_id,
                    entry["item_code"],
                    entry["item_name"],
                    entry["category"],
                    entry["uom"],
                    entry["quantity"],
                    entry["unit_price"],
                    entry["total_price"],
                    entry["purchased_at"],
                )
                for entry in entries
            ],
        )
        connection.execute("DELETE FROM cart_items WHERE owner_key = ?", (owner_key,))


def get_purchase_history(database_path, customer_id=None):
    query = """
        SELECT customer_id, item_code, item_name, category, uom, quantity,
               unit_price, total_price, purchased_at
        FROM purchase_history
    """
    values = ()
    if customer_id is not None:
        query += " WHERE customer_id = ?"
        values = (customer_id,)
    query += " ORDER BY purchased_at, id"

    with connect_database(database_path) as connection:
        return connection.execute(query, values).fetchall()
