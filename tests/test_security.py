import tempfile
import unittest
from pathlib import Path

import app as app_module
from database import connect_database, get_cart_items, get_purchase_history, initialize_database
from werkzeug.security import check_password_hash


class SecurityAndStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        temp_path = Path(self.temp_directory.name)
        app_module.DATABASE_PATH = temp_path / 'test.sqlite3'
        app_module.CUSTOMER_IMAGE_DIR = temp_path / 'customer_images'
        initialize_database(app_module.DATABASE_PATH)
        app_module.app.config.update(TESTING=True, SECRET_KEY='test-only-secret')
        self.client = app_module.app.test_client()

    def tearDown(self):
        self.temp_directory.cleanup()

    @staticmethod
    def signup_data(username='demo_user', email='demo@example.test'):
        return {
            'firstName': 'Demo',
            'lastName': 'User',
            'gender': 'Others',
            'dob': '1990-01-01',
            'country': 'Germany',
            'city': 'Darmstadt',
            'address': 'Example Street 1',
            'email': email,
            'username': username,
            'password': 'Synthetic-password-123',
            'repeatPassword': 'Synthetic-password-123',
        }

    def signup(self, client=None, **overrides):
        client = client or self.client
        data = self.signup_data()
        data.update(overrides)
        return client.post('/signup', data=data)

    def login(self, client=None, username='demo_user', password='Synthetic-password-123'):
        client = client or self.client
        return client.post('/api/login', json={'username': username, 'password': password})

    def test_database_initialization_and_password_hashing(self):
        response = self.signup()
        self.assertEqual(response.status_code, 200)

        with connect_database(app_module.DATABASE_PATH) as connection:
            tables = {
                row['name']
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            user = connection.execute(
                "SELECT * FROM users WHERE username = ?", ('demo_user',)
            ).fetchone()

        self.assertTrue({'users', 'cart_items', 'purchase_history'} <= tables)
        self.assertNotEqual(user['password_hash'], 'Synthetic-password-123')
        self.assertTrue(check_password_hash(user['password_hash'], 'Synthetic-password-123'))

    def test_duplicate_username_and_email_are_rejected(self):
        self.assertEqual(self.signup().status_code, 200)
        self.assertEqual(
            self.signup(username='demo_user', email='another@example.test').status_code,
            400,
        )
        self.assertEqual(
            self.signup(username='another_user', email='demo@example.test').status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                '/api/check-duplicate', json={'username': 'DEMO_USER'}
            ).status_code,
            400,
        )

    def test_signup_rejects_invalid_password_confirmation(self):
        self.assertEqual(
            self.signup(repeatPassword='different-password').status_code, 400
        )
        self.assertEqual(
            self.signup(password='short', repeatPassword='short').status_code, 400
        )

    def test_login_session_and_logout(self):
        self.signup()
        self.assertEqual(self.login(password='wrong-password').status_code, 401)
        self.assertEqual(self.login().status_code, 200)

        with self.client.session_transaction() as flask_session:
            self.assertEqual(set(flask_session.keys()), {'user_id'})
            self.assertNotIn('password', flask_session)
            self.assertNotIn('password_hash', flask_session)

        self.assertEqual(self.client.get('/home').status_code, 200)
        self.assertEqual(self.client.get('/logout').status_code, 302)
        with self.client.session_transaction() as flask_session:
            self.assertEqual(dict(flask_session), {})
        self.assertEqual(self.client.get('/home').status_code, 302)

    def test_carts_are_isolated_per_user_and_purchase_is_saved(self):
        first_client = self.client
        second_client = app_module.app.test_client()
        self.assertEqual(self.signup(client=first_client).status_code, 200)
        self.assertEqual(
            self.signup(
                client=second_client,
                username='second_user',
                email='second@example.test',
            ).status_code,
            200,
        )
        self.assertEqual(self.login(client=first_client).status_code, 200)
        self.assertEqual(
            self.login(client=second_client, username='second_user').status_code,
            200,
        )

        item_code = int(app_module.items[0]['Item_Code'])
        self.assertEqual(
            first_client.post('/api/add-to-cart', json={'item_code': item_code}).status_code,
            200,
        )
        self.assertFalse(first_client.get('/api/cart-status').get_json()['is_cart_empty'])
        self.assertTrue(second_client.get('/api/cart-status').get_json()['is_cart_empty'])

        with first_client.session_transaction() as first_session:
            first_user_id = first_session['user_id']
        with second_client.session_transaction() as second_session:
            second_user_id = second_session['user_id']

        self.assertEqual(
            len(get_cart_items(app_module.DATABASE_PATH, f'user:{first_user_id}')), 1
        )
        self.assertEqual(
            len(get_cart_items(app_module.DATABASE_PATH, f'user:{second_user_id}')), 0
        )

        save_response = first_client.post('/api/save-cart')
        self.assertEqual(save_response.status_code, 200)
        self.assertEqual(len(get_purchase_history(app_module.DATABASE_PATH, first_user_id)), 1)
        self.assertEqual(
            len(get_cart_items(app_module.DATABASE_PATH, f'user:{first_user_id}')), 0
        )
        self.assertEqual(first_client.get('/user').status_code, 200)
        self.assertEqual(first_client.get('/qr_code').status_code, 200)
        self.assertEqual(first_client.get('/user/line-chart').status_code, 200)
        self.assertEqual(first_client.get('/user/bar-chart').status_code, 200)
        self.assertEqual(first_client.get('/user/pie-chart').status_code, 200)
        self.assertEqual(first_client.get('/market_analysis').status_code, 200)


if __name__ == '__main__':
    unittest.main()
