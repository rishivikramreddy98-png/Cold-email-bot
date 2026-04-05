from sqlalchemy import Column, Integer, String, Text, ForeignKey, DateTime, Float, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, sessionmaker
from sqlalchemy import create_engine
import datetime

Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    telegram_id = Column(Integer, unique=True, nullable=False)
    smtp_email = Column(String)
    smtp_password = Column(String)
    imap_enabled = Column(Boolean, default=False)
    smtp_host = Column(String)
    smtp_port = Column(Integer)
    imap_host = Column(String)
    imap_port = Column(Integer)
    is_blocked = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    leads = relationship("Lead", back_populates="user")
    templates = relationship("Template", back_populates="user")
    deals = relationship("Deal", back_populates="user")
    campaigns = relationship("Campaign", back_populates="user")
    subscription = relationship("Subscription", back_populates="user", uselist=False)
    payments = relationship("Payment", back_populates="user")

class Lead(Base):
    __tablename__ = 'leads'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    email = Column(String, nullable=False)
    first_name = Column(String)
    last_name = Column(String)
    company = Column(String)
    industry = Column(String)
    location = Column(String)
    source = Column(String)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    user = relationship("User", back_populates="leads")
    deals = relationship("Deal", back_populates="lead")
    campaign_logs = relationship("CampaignLog", back_populates="lead")

class Template(Base):
    __tablename__ = 'templates'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    name = Column(String, nullable=False)
    subject = Column(String, nullable=False)
    body = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    user = relationship("User", back_populates="templates")

class Deal(Base):
    __tablename__ = 'deals'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    lead_id = Column(Integer, ForeignKey('leads.id'))
    title = Column(String, nullable=False)
    value = Column(Float, default=0.0)
    stage = Column(String, default="prospect")
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)
    
    user = relationship("User", back_populates="deals")
    lead = relationship("Lead", back_populates="deals")

class Campaign(Base):
    __tablename__ = 'campaigns'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    name = Column(String, nullable=False)
    template_id = Column(Integer, ForeignKey('templates.id'))
    status = Column(String, default="pending")
    follow_up_stage = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    user = relationship("User", back_populates="campaigns")
    logs = relationship("CampaignLog", back_populates="campaign")

class CampaignLog(Base):
    __tablename__ = 'campaign_logs'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    campaign_id = Column(Integer, ForeignKey('campaigns.id'))
    lead_id = Column(Integer, ForeignKey('leads.id'))
    status = Column(String)
    error_message = Column(Text)
    follow_up_stage = Column(Integer, default=1)
    sent_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    user = relationship("User")
    campaign = relationship("Campaign", back_populates="logs")
    lead = relationship("Lead", back_populates="campaign_logs")

class Subscription(Base):
    __tablename__ = 'subscriptions'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), unique=True, nullable=False)
    plan_name = Column(String, nullable=False)
    start_date = Column(DateTime, default=datetime.datetime.utcnow)
    end_date = Column(DateTime)
    emails_sent_today = Column(Integer, default=0)
    daily_limit = Column(Integer, default=0)
    max_campaigns = Column(Integer, default=1)
    is_active = Column(Boolean, default=False)

    user = relationship("User", back_populates="subscription")

class Payment(Base):
    __tablename__ = 'payments'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    amount = Column(Float, nullable=False)
    plan = Column(String, nullable=False)
    payment_proof = Column(String)
    status = Column(String, default='pending')
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    user = relationship("User", back_populates="payments")

def init_db(db_url):
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)
