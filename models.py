from sqlalchemy import Column, Integer, String, Text, ForeignKey, DateTime, Float, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, sessionmaker
from sqlalchemy import create_engine
import datetime
import enum

Base = declarative_base()

class DealStage(enum.Enum):
    PROSPECT = "prospect"
    QUALIFIED = "qualified"
    PROPOSAL = "proposal"
    NEGOTIATION = "negotiation"
    CLOSED_WON = "closed won"
    CLOSED_LOST = "closed lost"

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
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    leads = relationship("Lead", back_populates="user")
    templates = relationship("Template", back_populates="user")
    deals = relationship("Deal", back_populates="user")
    campaigns = relationship("Campaign", back_populates="user")

class Lead(Base):
    __tablename__ = 'leads'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    email = Column(String, nullable=False)
    first_name = Column(String)
    last_name = Column(String)
    company = Column(String)
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
    body = Column(Text, nullable=False) # Supports placeholders like {first_name}, {company}

    user = relationship("User", back_populates="templates")

class Deal(Base):
    __tablename__ = 'deals'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    lead_id = Column(Integer, ForeignKey('leads.id'))
    title = Column(String, nullable=False)
    value = Column(Float, default=0.0)
    stage = Column(String, default=DealStage.PROSPECT.value)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)
    
    user = relationship("User", back_populates="deals")
    lead = relationship("Lead", back_populates="deals")

class Campaign(Base):
    __tablename__ = 'campaigns'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    name = Column(String, nullable=False)
    template_id = Column(Integer, ForeignKey('templates.id'))
    status = Column(String, default="pending") # pending, running, completed, paused
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    user = relationship("User", back_populates="campaigns")
    logs = relationship("CampaignLog", back_populates="campaign")

class CampaignLog(Base):
    __tablename__ = 'campaign_logs'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    campaign_id = Column(Integer, ForeignKey('campaigns.id'))
    lead_id = Column(Integer, ForeignKey('leads.id'))
    status = Column(String) # sent, failed
    error_message = Column(Text)
    sent_at = Column(DateTime, default=datetime.datetime.utcnow)
    
    user = relationship("User") # No back_populates needed here
    campaign = relationship("Campaign", back_populates="logs")
    lead = relationship("Lead", back_populates="campaign_logs")

def init_db(db_url):
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)
