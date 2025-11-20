import os

class Config:
    SECRET_KEY = 'dev-key-full'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_DATABASE_URI = 'sqlite:///' + os.path.join(os.path.dirname(__file__), 'instance', 'app.db')
    VAT_RATE = 0.12
