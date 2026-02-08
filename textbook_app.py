#!/usr/bin/env python3
"""
TextbookSaver - Price Comparison Website
Find the cheapest textbooks across Amazon, eBay, and more

Requirements:
pip install flask flask-login flask-sqlalchemy stripe requests python-dotenv
"""

from flask import Flask, render_template, request, jsonify, redirect, url_for, session
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta
import requests
import os
from dotenv import load_dotenv
import stripe

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///textbooksaver.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Initialize extensions
db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# Stripe configuration
stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
STRIPE_PRICE_ID = os.getenv('STRIPE_PRICE_ID')  # Premium subscription price ID

# API Keys
EBAY_API_KEY = os.getenv('EBAY_API_KEY')
AMAZON_ASSOCIATE_TAG = os.getenv('AMAZON_ASSOCIATE_TAG')

# Models
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200))
    is_premium = db.Column(db.Boolean, default=False)
    stripe_customer_id = db.Column(db.String(100))
    premium_expires = db.Column(db.DateTime)
    searches_today = db.Column(db.Integer, default=0)
    last_search_reset = db.Column(db.DateTime, default=datetime.utcnow)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
    
    def can_search(self):
        """Check if user can make another search"""
        # Reset counter if it's a new day
        if self.last_search_reset.date() < datetime.utcnow().date():
            self.searches_today = 0
            self.last_search_reset = datetime.utcnow()
            db.session.commit()
        
        # Premium users have unlimited searches
        if self.is_premium and self.premium_expires > datetime.utcnow():
            return True
        
        # Free users limited to 5 searches per day
        return self.searches_today < 5
    
    def increment_search(self):
        """Increment search counter"""
        if not (self.is_premium and self.premium_expires > datetime.utcnow()):
            self.searches_today += 1
            db.session.commit()

class SavedSearch(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    isbn = db.Column(db.String(20))
    title = db.Column(db.String(300))
    best_price = db.Column(db.Float)
    best_source = db.Column(db.String(50))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Book Search APIs
class BookPriceFinder:
    def __init__(self):
        self.ebay_api_key = EBAY_API_KEY
        self.amazon_tag = AMAZON_ASSOCIATE_TAG
    
    def search_ebay(self, query, isbn=None):
        """Search eBay for textbooks"""
        if not self.ebay_api_key:
            return []
        
        search_term = isbn if isbn else query
        
        params = {
            'OPERATION-NAME': 'findItemsAdvanced',
            'SERVICE-VERSION': '1.0.0',
            'SECURITY-APPNAME': self.ebay_api_key,
            'RESPONSE-DATA-FORMAT': 'JSON',
            'REST-PAYLOAD': '',
            'keywords': search_term,
            'paginationInput.entriesPerPage': '10',
            'itemFilter(0).name': 'ListingType',
            'itemFilter(0).value': 'FixedPrice',
            'sortOrder': 'PricePlusShippingLowest'
        }
        
        try:
            response = requests.get(
                'https://svcs.ebay.com/services/search/FindingService/v1',
                params=params,
                timeout=10
            )
            data = response.json()
            
            items = data.get('findItemsAdvancedResponse', [{}])[0].get('searchResult', [{}])[0].get('item', [])
            
            results = []
            for item in items[:5]:  # Top 5 results
                try:
                    price = float(item['sellingStatus'][0]['currentPrice'][0]['__value__'])
                    results.append({
                        'source': 'eBay',
                        'price': price,
                        'title': item['title'][0],
                        'condition': item.get('condition', [{}])[0].get('conditionDisplayName', ['Used'])[0],
                        'url': item['viewItemURL'][0],
                        'shipping': item.get('shippingInfo', [{}])[0].get('shippingServiceCost', [{}])[0].get('__value__', '0'),
                        'image': item.get('galleryURL', [''])[0]
                    })
                except (KeyError, IndexError, ValueError):
                    continue
            
            return results
        except Exception as e:
            print(f"eBay search error: {e}")
            return []
    
    def search_amazon(self, query, isbn=None):
        """
        Search Amazon for textbooks
        Note: Amazon Product Advertising API requires approval and setup
        This is a placeholder that returns mock data for demo
        """
        # In production, you'd use the Amazon PA API here
        # For now, return placeholder data
        search_term = isbn if isbn else query
        
        # Amazon affiliate link builder
        def build_amazon_link(search_term):
            base_url = "https://www.amazon.com/s"
            params = f"?k={search_term.replace(' ', '+')}"
            if self.amazon_tag:
                params += f"&tag={self.amazon_tag}"
            return base_url + params
        
        # Placeholder - replace with actual API call
        return [{
            'source': 'Amazon',
            'price': 0,  # Will show as "Check Amazon"
            'title': f'Search results for: {query}',
            'condition': 'Various',
            'url': build_amazon_link(search_term),
            'is_search_link': True
        }]
    
    def search_all(self, query, isbn=None):
        """Search all platforms and combine results"""
        all_results = []
        
        # Search eBay
        ebay_results = self.search_ebay(query, isbn)
        all_results.extend(ebay_results)
        
        # Search Amazon
        amazon_results = self.search_amazon(query, isbn)
        all_results.extend(amazon_results)
        
        # Sort by price (lowest first), excluding search links
        priced_results = [r for r in all_results if r.get('price', 0) > 0]
        search_links = [r for r in all_results if r.get('is_search_link')]
        
        priced_results.sort(key=lambda x: x['price'])
        
        return priced_results + search_links

# Routes
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/search', methods=['POST'])
@login_required
def search():
    """Handle book search"""
    if not current_user.can_search():
        return jsonify({
            'error': 'Daily search limit reached. Upgrade to Premium for unlimited searches!'
        }), 403
    
    data = request.json
    query = data.get('query', '').strip()
    isbn = data.get('isbn', '').strip()
    
    if not query and not isbn:
        return jsonify({'error': 'Please provide a book title or ISBN'}), 400
    
    finder = BookPriceFinder()
    results = finder.search_all(query, isbn)
    
    current_user.increment_search()
    
    # Calculate savings
    if len(results) > 1:
        priced = [r for r in results if r.get('price', 0) > 0]
        if priced:
            cheapest = min(r['price'] for r in priced)
            most_expensive = max(r['price'] for r in priced)
            savings = most_expensive - cheapest
        else:
            savings = 0
    else:
        savings = 0
    
    return jsonify({
        'results': results,
        'savings': savings,
        'searches_remaining': max(0, 5 - current_user.searches_today) if not current_user.is_premium else 'unlimited'
    })

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        data = request.json
        email = data.get('email')
        password = data.get('password')
        
        if User.query.filter_by(email=email).first():
            return jsonify({'error': 'Email already registered'}), 400
        
        user = User(email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        
        login_user(user)
        return jsonify({'success': True})
    
    return render_template('signup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        data = request.json
        email = data.get('email')
        password = data.get('password')
        
        user = User.query.filter_by(email=email).first()
        
        if user and user.check_password(password):
            login_user(user)
            return jsonify({'success': True})
        
        return jsonify({'error': 'Invalid email or password'}), 401
    
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('index'))

@app.route('/create-checkout-session', methods=['POST'])
@login_required
def create_checkout_session():
    """Create Stripe checkout session for premium subscription"""
    try:
        checkout_session = stripe.checkout.Session.create(
            customer_email=current_user.email,
            payment_method_types=['card'],
            line_items=[{
                'price': STRIPE_PRICE_ID,
                'quantity': 1,
            }],
            mode='subscription',
            success_url=url_for('payment_success', _external=True),
            cancel_url=url_for('index', _external=True),
        )
        return jsonify({'checkout_url': checkout_session.url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/payment-success')
@login_required
def payment_success():
    """Handle successful payment"""
    current_user.is_premium = True
    current_user.premium_expires = datetime.utcnow() + timedelta(days=30)
    db.session.commit()
    return render_template('success.html')

@app.route('/webhook', methods=['POST'])
def stripe_webhook():
    """Handle Stripe webhooks for subscription events"""
    payload = request.data
    sig_header = request.headers.get('Stripe-Signature')
    
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, os.getenv('STRIPE_WEBHOOK_SECRET')
        )
    except ValueError:
        return '', 400
    except stripe.error.SignatureVerificationError:
        return '', 400
    
    # Handle subscription events
    if event['type'] == 'customer.subscription.deleted':
        # Subscription cancelled
        customer_email = event['data']['object']['customer_email']
        user = User.query.filter_by(email=customer_email).first()
        if user:
            user.is_premium = False
            db.session.commit()
    
    return '', 200

@app.route('/dashboard')
@login_required
def dashboard():
    """User dashboard"""
    return render_template('dashboard.html', user=current_user)

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    
    print("\n" + "="*60)
    print("📚 TEXTBOOKSAVER - Price Comparison Website")
    print("="*60)
    print("\n✅ Server starting...")
    print("👉 Open: http://localhost:5000")
    print("\nPress Ctrl+C to stop")
    print("="*60 + "\n")
    
    app.run(debug=True, port=5000)
