from pathlib import Path
from functools import wraps
import os
import secrets
import sqlite3
import threading

BASE_DIR = Path(__file__).resolve().parent
os.environ.setdefault("MPLCONFIGDIR", str(BASE_DIR / ".matplotlib_cache"))

from flask import Flask, jsonify, render_template, request,redirect, url_for, session,send_file
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg') 
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patheffects import withStroke
from matplotlib.colors import LinearSegmentedColormap
import matplotlib.cm as cm
import io
import base64
from PIL import Image, ImageDraw, ImageFont
import qrcode
from datetime import datetime
from werkzeug.security import check_password_hash, generate_password_hash

from database import (
    add_cart_item as db_add_cart_item,
    clear_cart as db_clear_cart,
    create_user,
    get_cart_items as db_get_cart_items,
    get_purchase_history,
    get_user_by_id,
    get_user_by_username,
    initialize_database,
    remove_cart_item as db_remove_cart_item,
    save_purchase,
    update_cart_quantity as db_update_cart_quantity,
    update_user_image,
    user_exists,
)


app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('FLASK_SECRET_KEY', 'dev-only-change-me')
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('FLASK_COOKIE_SECURE', '').lower() == 'true'

if 'FLASK_SECRET_KEY' not in os.environ:
    app.logger.warning(
        'Using the development-only Flask secret key. Set FLASK_SECRET_KEY outside local development.'
    )

DATASETS_DIR = BASE_DIR / 'Datasets'
STATIC_DIR = BASE_DIR / 'static'
DATABASE_PATH = Path(os.environ.get('GROCETRA_DB_PATH', BASE_DIR / 'instance' / 'grocetra.sqlite3'))
CUSTOMER_IMAGE_DIR = STATIC_DIR / 'images' / 'customers'

# Load required datasets for the App
FULL_ITEM_INFO_EXCEL_FILE = STATIC_DIR / 'Files' / 'Full_Item_Info.xlsx'
WEB_SCRAPING_EXCEL_FILE = DATASETS_DIR / 'Web_Scraping' / 'Web_Scraping.xlsx'
data = pd.read_excel(FULL_ITEM_INFO_EXCEL_FILE, sheet_name='Sheet1')
initialize_database(DATABASE_PATH)


# Convert Item's informaion to a dictionary for easy access
items = data.to_dict(orient='records')
PLOT_LOCK = threading.RLock()


def synchronized_plot(function):
    """Serialize Matplotlib rendering across Flask request threads."""
    @wraps(function)
    def wrapper(*args, **kwargs):
        with PLOT_LOCK:
            return function(*args, **kwargs)
    return wrapper


def serialize_user(user):
    """Return the existing template/API shape without exposing the password hash."""
    if user is None:
        return None
    return {
        'Customer_ID': user['customer_id'],
        'FirstName': user['first_name'],
        'LastName': user['last_name'],
        'Username': user['username'],
        'Points': user['points'],
        'Image': user['image'],
    }


def current_user():
    customer_id = session.get('user_id')
    if customer_id is None:
        return None
    user = get_user_by_id(DATABASE_PATH, customer_id)
    if user is None:
        session.clear()
    return user


def cart_owner_key():
    customer_id = session.get('user_id')
    if customer_id is not None:
        return f'user:{customer_id}'
    if 'cart_id' not in session:
        session['cart_id'] = secrets.token_urlsafe(18)
    return f"guest:{session['cart_id']}"


def cart_items_for_request():
    cart_rows = db_get_cart_items(DATABASE_PATH, cart_owner_key())
    detailed_items = []
    for cart_row in cart_rows:
        item = next((entry for entry in items if entry['Item_Code'] == cart_row['item_code']), None)
        if item:
            detailed_items.append({
                'item_code': item['Item_Code'],
                'name': item['Item_name_in_English'],
                'category': item['Category'],
                'uom': item['UOM'],
                'image': item['Image'],
                'Rewe': item['Rewe'],
                'Netto': item['Netto'],
                'Penny': item['Penny'],
                'Kaufland': item['Kaufland'],
                'AlDI': item['AlDI'],
                'quantity': cart_row['quantity'],
            })
    return detailed_items


def cart_totals(cart_items):
    return {
        market: sum(item[market] * item['quantity'] for item in cart_items)
        for market in ('Rewe', 'Netto', 'Penny', 'Kaufland', 'AlDI')
    }


def purchase_history_dataframe(customer_id=None):
    rows = get_purchase_history(DATABASE_PATH, customer_id)
    columns = [
        'Customer_ID', 'Item_Code', 'Item_Name', 'Category', 'UOM',
        'Quantity', 'Unit Price', 'Total Price', 'Date'
    ]
    return pd.DataFrame(
        [
            {
                'Customer_ID': row['customer_id'],
                'Item_Code': row['item_code'],
                'Item_Name': row['item_name'],
                'Category': row['category'],
                'UOM': row['uom'],
                'Quantity': row['quantity'],
                'Unit Price': row['unit_price'],
                'Total Price': row['total_price'],
                'Date': row['purchased_at'],
            }
            for row in rows
        ],
        columns=columns,
    )

# Function to generate the chart for the variable price across different supermarket
@synchronized_plot
def generate_chart_inline(total_prices):
    supermarkets = list(total_prices.keys())
    prices = list(total_prices.values())

    plt.figure(figsize=(10, 6))
    bars = plt.bar(supermarkets, prices, color=['#6c3483', '#2980b9', '#27ae60', '#f39c12', '#e74c3c'])

    for bar, price in zip(bars, prices):
        plt.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.1, f"€{price:.2f}", ha='center', fontsize=10)


    plt.ylabel('Total Price (€)', fontsize=12)
    plt.grid(axis='y', linestyle='--', alpha=0.7)
    plt.tight_layout()

    # Convert the plot to a base64-encoded image
    img = io.BytesIO()
    plt.savefig(img, format='png')
    img.seek(0)
    plot_url = base64.b64encode(img.getvalue()).decode('utf8')
    plt.close()

    return plot_url


# Default route for the App
@app.route('/')
def login_page():
    return render_template('login.html')


# Route for Login Page
@app.route('/api/login', methods=['POST'])
def api_login():
    request_data = request.get_json(silent=True) or {}
    username = str(request_data.get('username', '')).strip()
    password = str(request_data.get('password', ''))

    if not username or not password:
        return jsonify({'success': False, 'message': 'Username and password are required'}), 400

    user = get_user_by_username(DATABASE_PATH, username)
    if user is None or not check_password_hash(user['password_hash'], password):
        return jsonify({'success': False, 'message': 'Invalid username or password'}), 401

    session.clear()
    session['user_id'] = user['customer_id']
    return jsonify({'success': True, 'message': 'Login successful! '})


# Route for Home Page
@app.route('/home')
def home():
    user = current_user()
    if user:
        return render_template('home.html', user=serialize_user(user))
    return redirect(url_for('login_page'))

# Logout Action
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login_page'))


# Route for Signup Page
@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        first_name = request.form.get('firstName', '').strip()
        last_name = request.form.get('lastName', '').strip()
        gender = request.form.get('gender', '').strip()
        dob = request.form.get('dob', '').strip()
        country = request.form.get('country', '').strip()
        city = request.form.get('city', '').strip()
        address = request.form.get('address', '').strip()
        email = request.form.get('email', '').strip()
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        repeat_password = request.form.get('repeatPassword', '')

        required_fields = (
            first_name, last_name, gender, dob, country, city, address,
            email, username, password, repeat_password,
        )
        if not all(required_fields):
            return jsonify({'success': False, 'message': 'All fields are required.'}), 400
        if password != repeat_password:
            return jsonify({'success': False, 'message': 'Passwords do not match!'}), 400
        if len(password) < 8:
            return jsonify({'success': False, 'message': 'Password must be at least 8 characters.'}), 400

        if user_exists(DATABASE_PATH, email=email):
            return jsonify({'success': False, 'message': 'Email already taken!'}), 400
        if user_exists(DATABASE_PATH, username=username):
            return jsonify({'success': False, 'message': 'Username already taken!'}), 400

        try:
            customer_id = create_user(DATABASE_PATH, {
                'first_name': first_name,
                'last_name': last_name,
                'gender': gender,
                'date_of_birth': dob,
                'country': country,
                'city': city,
                'address': address,
                'email': email,
                'username': username,
                'password_hash': generate_password_hash(password),
                'points': 0,
                'image': None,
            })
        except sqlite3.IntegrityError:
            return jsonify({'success': False, 'message': 'Email or username already taken!'}), 400

        # Geberate QR Code for new customer
        qr_data = f"https://www.example.com\nGroCetra\nCustomer ID : {customer_id}"
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data(qr_data)
        qr.make(fit=True)

        qr_image = qr.make_image(fill_color="black", back_color="white").convert("RGB")

        # Add text below the QR code
        width, height = qr_image.size
        new_height = height + 150  # Increase height for text
        img_with_text = Image.new("RGB", (width, new_height), "white")
        img_with_text.paste(qr_image, (0, 0))

        draw = ImageDraw.Draw(img_with_text)
        try:
            font = ImageFont.truetype("arial.ttf", size=20)
        except OSError:
            font = ImageFont.load_default()
        text = f"GroCetra\nCustomer ID: {customer_id}"

        # Calculate text size using textbbox()
        text_bbox = draw.multiline_textbbox((0, 0), text, font=font)
        text_width = text_bbox[2] - text_bbox[0]  # Text width
        text_height = text_bbox[3] - text_bbox[1]  # Text height

        # Center the text below the QR code
        text_x = (width - text_width) // 2
        text_y = height + 10
        draw.multiline_text((text_x, text_y), text, fill="black", font=font, align="center")

        # Save the QR code image
        qr_file_path = CUSTOMER_IMAGE_DIR / f'{customer_id}.png'
        qr_file_path.parent.mkdir(parents=True, exist_ok=True)
        img_with_text.save(qr_file_path)
        update_user_image(
            DATABASE_PATH, customer_id, f'/static/images/customers/{customer_id}.png'
        )

        return jsonify({'success': True, 'message': 'Welcome! Your account has been created successfully.'}), 200

    return render_template('signup.html')


# Check Duplicate Email or Username
@app.route('/api/check-duplicate', methods=['POST'])
def check_duplicate():
    request_data = request.get_json(silent=True) or {}
    email = str(request_data.get('email', '')).strip()
    username = str(request_data.get('username', '')).strip()

    if email and user_exists(DATABASE_PATH, email=email):
        return jsonify({'success': False, 'message': 'This Email has already taken!'}), 400

    if username and user_exists(DATABASE_PATH, username=username):
        return jsonify({'success': False, 'message': 'This Username hase already taken!'}), 400

    return jsonify({'success': True}), 200


# Get Item Details
@app.route('/api/items')
def get_items():
    """API endpoint to fetch all items."""
    formatted_items = [
        {
            'item_code':item['Item_Code'],
            'name': item['Item_name_in_English'],
            'category': item['Category'],
            'uom': item['UOM'],
            'today_price': item['Rewe'],
            'tomorrow_price': item['Price_for_Tomorrow'],
            'next_week_price': item['Price_After_7_Days'],
            'next_month_price': item['Price_After_1_Month'],
            'image': item['Image'],
            'plot_image': item['Plot_Image'],            
            'Rewe': item['Rewe'],
            'Netto': item['Netto'],
            'Penny': item['Penny'],
            'Kaufland': item['Kaufland'],
            'ALDI': item['AlDI'],            
            'description': item['Description'] if pd.notna(item['Description']) else 'No description available.',
            'benefits': item['Benefits'] if pd.notna(item['Benefits']) else 'No benefits information available.'
        }
        for item in items
    ]
    return jsonify(formatted_items)


# Add Items to Cart Action
@app.route('/api/add-to-cart', methods=['POST'])
def add_to_cart():
    """API endpoint to add items to the cart."""
    request_data = request.get_json(silent=True) or {}
    try:
        item_code = int(request_data['item_code'])
    except (KeyError, TypeError, ValueError):
        return jsonify({'error': 'Invalid request data'}), 400

    item = next((entry for entry in items if entry['Item_Code'] == item_code), None)
    if item is None:
        return jsonify({'error': 'Item not found'}), 404

    db_add_cart_item(DATABASE_PATH, cart_owner_key(), item_code)
    return jsonify({'message': 'Item added to cart successfully!'}), 200

# Update Cart Items Action
@app.route('/api/update-quantity', methods=['POST'])
def update_quantity():
    request_data = request.get_json(silent=True) or {}
    try:
        item_code = int(request_data['item_code'])
        change = int(request_data['change'])
    except (KeyError, TypeError, ValueError):
        return jsonify({'error': 'Invalid request data'}), 400

    db_update_cart_quantity(DATABASE_PATH, cart_owner_key(), item_code, change)
    cart_items = cart_items_for_request()

    # Recalculate the total prices and generate the updated chart
    total_prices = cart_totals(cart_items)

    chart_url = generate_chart_inline(total_prices)
    lowest_price_supermarket = min(total_prices, key=total_prices.get)
    lowest_price = total_prices[lowest_price_supermarket]
    

    return jsonify({
        'message': 'Quantity updated successfully!',
        'new_chart_url': chart_url,
        'lowest_price_supermarket': lowest_price_supermarket,
        'lowest_price': lowest_price
    }), 200

# Remove Items from Cart
@app.route('/api/remove-from-cart', methods=['POST'])
def remove_from_cart():
    try:
        request_data = request.get_json(silent=True) or {}
        item_code = int(request_data['item_code'])
        db_remove_cart_item(DATABASE_PATH, cart_owner_key(), item_code)
        cart_items = cart_items_for_request()

        # Recalculate the total prices and generate the updated chart and text
        total_prices = cart_totals(cart_items)

        chart_url = generate_chart_inline(total_prices)
        lowest_price_supermarket = min(total_prices, key=total_prices.get)
        lowest_price = total_prices[lowest_price_supermarket]

        return jsonify({
            'message': 'Item removed successfully!',
            'new_chart_url': chart_url,
            'lowest_price_supermarket': lowest_price_supermarket,
            'lowest_price': lowest_price
        }), 200

    except (KeyError, TypeError, ValueError):
        return jsonify({'message': 'Invalid request data.'}), 400
    except sqlite3.Error:
        app.logger.exception('Could not remove an item from the cart')
        return jsonify({'message': 'An error occurred while removing the item.'}), 500

# Check Cart Status
@app.route('/api/cart-status', methods=['GET'])
def cart_status():
    """API endpoint to check if the cart is empty."""
    return jsonify({'is_cart_empty': len(cart_items_for_request()) == 0})

# Route for Cart Page
@app.route('/cart')
def cart():
    detailed_cart_items = cart_items_for_request()

    # Calculate total prices
    total_prices = cart_totals(detailed_cart_items)

    # Generate the comparison chart
    chart_url = generate_chart_inline(total_prices)

    # Find the lowest price
    lowest_price_supermarket = min(total_prices, key=total_prices.get)
    lowest_price = total_prices[lowest_price_supermarket]

    return render_template('cart.html',
                           cart_items=detailed_cart_items,
                           chart_url=chart_url,
                           lowest_price_supermarket=lowest_price_supermarket,
                           lowest_price=lowest_price)


# Clear All Items from Cart
@app.route('/api/clear-cart', methods=['POST'])
def clear_cart():
    """Clear all items from the cart."""
    db_clear_cart(DATABASE_PATH, cart_owner_key())
    return jsonify({'message': 'Cart cleared successfully!'}), 200

# Pass the information of Cart Items to Purchase History Database
@app.route('/api/save-cart', methods=['POST'])
def save_cart():
    """Save the current user's cart and points in one SQLite transaction."""
    user = current_user()
    if not user:
        return jsonify({'success': False, 'message': 'User not logged in.'}), 401

    owner_key = cart_owner_key()
    cart_items = cart_items_for_request()
    if not cart_items:
        return jsonify({'success': False, 'message': 'Your cart is empty.'}), 400

    total_prices = cart_totals(cart_items)
    lowest_price_supermarket = min(total_prices, key=total_prices.get)
    new_points = int(float(total_prices[lowest_price_supermarket]))
    today_date = datetime.now().strftime('%Y-%m-%d')
    entries = [
        {
            'item_code': item['item_code'],
            'item_name': item['name'],
            'category': item['category'],
            'uom': item['uom'],
            'quantity': item['quantity'],
            'unit_price': item[lowest_price_supermarket],
            'total_price': item['quantity'] * item[lowest_price_supermarket],
            'purchased_at': today_date,
        }
        for item in cart_items
    ]
    save_purchase(
        DATABASE_PATH, user['customer_id'], owner_key, entries, new_points
    )

    return jsonify({'success': True, 'message': f'Thank you for providing the information. You will receive {new_points} points. Please scan your QR code at the Supermarket terminal to collect your points.'})


# Refresh User Session
@app.route('/api/refresh-session', methods=['GET'])
def refresh_session():
    user = current_user()
    if user:
        return jsonify({'success': True, 'user': serialize_user(user)})
    return jsonify({'success': False, 'message': 'User not logged in.'}), 401


#Route for QR Code
@app.route('/qr_code')
def qr_code():
    user = current_user()
    if user:
        return render_template('qr_code.html', qr_code_path=user['image'])
    return redirect(url_for('login_page'))


# Load User Details
@app.route('/user')
def user_dashboard():
    user = current_user()
    if user:
        template_user = serialize_user(user)
        template_user['ProfileImage'] = (
            f"/static/images/customers/profile_image/{user['customer_id']}.png"
        )
        return render_template('user.html', user=template_user)
    return redirect(url_for('login_page'))

# To show the User expenditure over the time.
@app.route('/user/line-chart')
@synchronized_plot
def user_line_chart():
    user = current_user()
    if user:
        customer_data = purchase_history_dataframe(user['customer_id'])
        if not customer_data.empty:
            customer_data['Date'] = pd.to_datetime(customer_data['Date'])
            customer_data['Month_Year'] = customer_data['Date'].dt.strftime('%B %Y')
            monthly_totals = customer_data.groupby('Month_Year')['Total Price'].sum()
            monthly_totals = monthly_totals.reindex(
                pd.to_datetime(monthly_totals.index, format='%B %Y').sort_values().strftime('%B %Y')
            )
            monthly_totals.index = pd.to_datetime(monthly_totals.index, format='%B %Y')

            fig, ax = plt.subplots(figsize=(14, 8), constrained_layout=True)
            x_values = monthly_totals.index
            y_values = monthly_totals.values
            colors = plt.cm.viridis(np.linspace(0, 1, len(y_values)))

            for i in range(len(x_values) - 1):
                ax.plot(x_values[i:i+2], y_values[i:i+2], color=colors[i], linewidth=3)
            ax.scatter(x_values, y_values, c=np.arange(len(y_values)), cmap='viridis', s=100, edgecolor='black')

            for x, y in zip(x_values, y_values):
                ax.text(x, y + max(y_values) * 0.02, f"€{y:.2f}", fontsize=10, ha='center', color='black')

            ax.grid(axis='y', linestyle='--', alpha=0.6)
            ax.spines['top'].set_visible(False)
            ax.spines['right'].set_visible(False)
            #ax.set_title('Total Spending Over Time', fontsize=18, fontweight='bold')
            ax.set_ylabel('Total Spending (in €)', fontsize=14, labelpad=10)

            # Customize the x-axis for better readability
            plt.gca().xaxis.set_major_formatter(mdates.DateFormatter('%b %Y'))
            plt.gca().xaxis.set_major_locator(mdates.MonthLocator())
            plt.xticks(rotation=0, fontsize=12)

            img = io.BytesIO()
            plt.savefig(img, format='png')
            img.seek(0)
            plt.close()
            return send_file(img, mimetype='image/png')
    return "No data available", 400

#To show the montly purchase history on different categories
@app.route('/user/bar-chart')
@synchronized_plot
def user_bar_chart():
    user = current_user()
    if user:
        customer_data = purchase_history_dataframe(user['customer_id'])
        if not customer_data.empty:
            customer_data['Date'] = pd.to_datetime(customer_data['Date'])
            customer_data['Month_Year'] = customer_data['Date'].dt.strftime('%B %Y')
            monthly_data = customer_data.groupby(['Month_Year', 'Category'])['Total Price'].sum().unstack()
            monthly_data = monthly_data.reindex(
                pd.to_datetime(monthly_data.index, format='%B %Y').sort_values().strftime('%B %Y'), axis=0
            )

            fig, ax = plt.subplots(figsize=(14, 8), constrained_layout=True)
            num_months = len(monthly_data.index)
            colors = cm.viridis(np.linspace(0.2, 0.9, num_months))
            monthly_data.T.plot(kind='bar', ax=ax, width=0.75, color=colors, edgecolor='black')

            #ax.set_title('Your Monthly Purchase History on Grocery Items', fontsize=18, fontweight='bold')
            ax.set_ylabel('Total Purchases (in €)', fontsize=14, labelpad=10)
            ax.set_xticks(range(len(monthly_data.columns)))
            ax.set_xticklabels(monthly_data.columns, rotation=0, fontsize=12)
            ax.grid(axis='y', linestyle='--', alpha=0.6)
            ax.spines['top'].set_visible(False)
            ax.spines['right'].set_visible(False)

            # Annotate bars with their values
            for bar_group in ax.containers:
                ax.bar_label(
                    bar_group,
                    fmt='€%.2f',
                    fontsize=10,
                    padding=3,
                    color='black'
                )

            img = io.BytesIO()
            plt.savefig(img, format='png')
            img.seek(0)
            plt.close()
            return send_file(img, mimetype='image/png')
    return "No data available", 400

# To show the user habits on grocery items
@app.route('/user/pie-chart')
@synchronized_plot
def user_pie_chart():
    user = current_user()
    if user:
        customer_data = purchase_history_dataframe(user['customer_id'])
        if not customer_data.empty:
            category_data = customer_data.groupby('Category')['Total Price'].sum()

            fig, ax = plt.subplots(figsize=(10, 10), constrained_layout=True)
            wedges, texts, autotexts = ax.pie(
                category_data, labels=category_data.index, autopct='%1.0f%%',
                startangle=90, colors=plt.cm.Set2.colors, textprops={'fontsize': 12},
                pctdistance=0.8, wedgeprops={'edgecolor': 'white', 'linewidth': 2} 
            )
            for autotext in autotexts:
                autotext.set_color('white')
                autotext.set_fontweight('bold')

            centre_circle = plt.Circle((0, 0), 0.60, fc='white')
            ax.add_artist(centre_circle)
            #ax.set_title('Your Spending Distribution on Grocery Products', fontsize=16, fontweight='bold')

            img = io.BytesIO()
            plt.savefig(img, format='png')
            img.seek(0)
            plt.close()
            return send_file(img, mimetype='image/png')
    return "No data available", 400


# To show the top 6 products
def get_top_products_by_quantity(top_n=6):
    purchase_history_data = purchase_history_dataframe()
    if purchase_history_data.empty:
        return pd.DataFrame(columns=['Item_Code', 'Quantity'])
    top_products = purchase_history_data.groupby('Item_Code', as_index=False)['Quantity'].sum()
    top_products = top_products.sort_values(by='Quantity', ascending=False).head(top_n)
    return top_products


# Route for Market Analysis Page
@app.route('/market_analysis')
def market_analysis():
    top_products_data = get_top_products_by_quantity(top_n=6)
    top_items = top_products_data['Item_Code'].tolist()

    # Get item details from `items` data
    top_products = []
    for item_code in top_items:
        item = next((i for i in items if i['Item_Code'] == item_code), None)  
        if item:
            top_products.append({
                'item_code': item_code,
                'name': item['Item_name_in_English'],
                'image': item['Image'],
                'quantity': int(top_products_data[top_products_data['Item_Code'] == item_code]['Quantity'].iloc[0])
            })

    return render_template('market_analysis.html', top_products=top_products)

#To show price variation in different supermarket
@app.route('/plot/<int:item_code>')
@synchronized_plot
def get_price_trend_plot(item_code):
    web_scraping_data = pd.read_excel(WEB_SCRAPING_EXCEL_FILE)

    # Filter web scraping data for the provided item code
    item_price_history_data = web_scraping_data[web_scraping_data['Item_Code'] == item_code]

    if item_price_history_data.empty:
        return "No data found for the given Item Code.", 404

    item_name = item_price_history_data['Item_name_in_English'].iloc[0]

    # Plot price trends
    plt.figure(figsize=(10, 6))
    for market in ['Rewe', 'Netto', 'Penny', 'Kaufland', 'AlDI']:
        if market in item_price_history_data.columns:
            plt.plot(
                item_price_history_data['Date'],
                item_price_history_data[market],
                label=market,
                marker='o',
                markersize=8,
                linewidth=2
            )

    plt.title(f'Price Trends for {item_name}', fontsize=18, fontweight='bold')
    #plt.xlabel('Date', fontsize=14)
    plt.ylabel('Price (€)', fontsize=14)
    plt.xticks(rotation=90)
    plt.legend(title="Markets", fontsize=12, loc='upper left', bbox_to_anchor=(1, 1))
    plt.grid(axis='y', linestyle='--', alpha=0.7)

    # Annotate max and min prices
    for market in ['Rewe', 'Netto', 'Penny', 'Kaufland', 'AlDI']:
        if market in item_price_history_data.columns:
            max_price = item_price_history_data[market].max()
            min_price = item_price_history_data[market].min()
            max_date = item_price_history_data['Date'][item_price_history_data[market].idxmax()]
            min_date = item_price_history_data['Date'][item_price_history_data[market].idxmin()]

            plt.text(
                max_date, max_price, f'High: €{max_price:.2f}',
                color='red', fontsize=10, ha='center', va='bottom',
                path_effects=[withStroke(linewidth=2, foreground='white')]
            )
            plt.text(
                min_date, min_price, f'Low: €{min_price:.2f}',
                color='green', fontsize=10, ha='center', va='top',
                path_effects=[withStroke(linewidth=2, foreground='white')]
            )

    plt.tight_layout()

    # Save the plot to a BytesIO object and encode as base64
    img = io.BytesIO()
    plt.savefig(img, format='png')
    img.seek(0)
    plt.close()

    return base64.b64encode(img.getvalue()).decode('utf-8')

# To show the Market Size
@app.route('/market-analysis-bar-chart')
@synchronized_plot
def market_analysis_bar_chart():
    market_data = purchase_history_dataframe()
    if market_data.empty:
        return "No data available", 400

    # Convert the 'Date' column to datetime format to extract month-year
    market_data['Date'] = pd.to_datetime(market_data['Date'])
    market_data['Month_Year'] = market_data['Date'].dt.strftime('%B %Y')  # Extract "Month Year"

    # Aggregate Total Price by Month-Year
    monthly_totals = market_data.groupby('Month_Year')['Total Price'].sum()

    # Sort months chronologically
    monthly_totals = monthly_totals.reindex(
        pd.to_datetime(monthly_totals.index, format='%B %Y').sort_values().strftime('%B %Y')
    )

    # Convert Month-Year back to datetime for plotting
    monthly_totals.index = pd.to_datetime(monthly_totals.index, format='%B %Y')

    # Calculate growth trends (percentage change)
    growth_trends = monthly_totals.pct_change() * 100

    # Generate the bar chart
    plt.figure(figsize=(14, 8))
    x_values = monthly_totals.index
    y_values = monthly_totals.values

    # Create bars with gradient color
    bar_colors = plt.cm.viridis(np.linspace(0, 1, len(y_values)))
    bars = plt.bar(x_values, y_values, color=bar_colors, edgecolor='black', width=20)

    # Add data labels to each bar
    for bar, y in zip(bars, y_values):
        plt.text(bar.get_x() + bar.get_width()/2, y + max(y_values)*0.02, f"€{y:.2f}", 
                 ha='center', fontsize=10, color='black')

    # Add growth trend labels
    for i, (x, y, growth) in enumerate(zip(x_values, y_values, growth_trends)):
        if not pd.isna(growth):
            trend_color = 'lime' if growth > 0 else 'red'
            plt.text(x, y - max(y_values)*0.1, f"{growth:+.1f}%", 
                     ha='center', fontsize=10, color=trend_color)

    # Customize the grid and spines
    plt.grid(axis='y', linestyle='--', alpha=0.6, color='gray')
    plt.gca().spines['top'].set_visible(False)
    plt.gca().spines['right'].set_visible(False)

    # Customize the x-axis for better readability
    plt.gca().xaxis.set_major_formatter(mdates.DateFormatter('%b %Y'))
    plt.gca().xaxis.set_major_locator(mdates.MonthLocator())
    plt.xticks(rotation=0, fontsize=12)

    # Add titles and labels
    #plt.title('Total Spending Over Time with Growth Trends', fontsize=18, fontweight='bold', color='#333333')
    plt.xlabel('', fontsize=14, labelpad=10)
    plt.ylabel('Total Value (in €)', fontsize=14, labelpad=10)

    # Add background gradient
    plt.gca().set_facecolor('#f7f7f7')

    # Serve as PNG
    img = io.BytesIO()
    plt.savefig(img, format='png')
    img.seek(0)
    plt.close()

    return send_file(img, mimetype='image/png')

# To show customer's spending nature
@app.route('/market-analysis-pie-chart')
@synchronized_plot
def market_analysis_pie_chart():
    market_data = purchase_history_dataframe()
    if market_data.empty:
        return "No data available", 400

    # Summarize data by Category and Total Price
    category_summary = market_data.groupby('Category')['Total Price'].sum()

    # Custom color palette
    colors = plt.cm.Paired(range(len(category_summary)))

    # Create a pie chart with visual effects
    plt.figure(figsize=(10, 10))
    explode = [0.05] * len(category_summary)  # Slightly explode all slices

    # Custom autopct function to show only the percentage
    def autopct_only_percentage(pct):
        return f"{pct:.1f}%"

    # Plot the pie chart
    plt.pie(
        category_summary,
        labels=[f"{category}\n(${value:.2f})" for category, value in category_summary.items()],
        autopct=autopct_only_percentage,
        startangle=90,
        shadow=True,
        explode=explode,
        colors=colors,
    )

    #plt.title('Buying Nature of Customers')

    # Serve as PNG
    img = io.BytesIO()
    plt.savefig(img, format='png')
    img.seek(0)
    plt.close()

    return send_file(img, mimetype='image/png')


if __name__ == '__main__':
    app.run(debug=True)
