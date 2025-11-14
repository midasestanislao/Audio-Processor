import streamlit as st
import assemblyai as aai
from pydub import AudioSegment
import os
import tempfile
import io
import zipfile
import sqlite3
import hashlib
import uuid
from pathlib import Path
from dotenv import load_dotenv
from datetime import datetime
import base64

# Load environment variables
load_dotenv()

# Configuration
STORAGE_DIR = Path("data/audio_storage")
DB_PATH = Path("data/conversations.db")

# Ensure directories exist
STORAGE_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# Get API key
api_key = '3853134a22f046c899000b66572f9d41'
if not api_key:
    st.error("⚠️ Configuration Error: API key not found.")
    st.stop()

aai.settings.api_key = api_key

# Page configuration
st.set_page_config(
    page_title="Audio Speaker Separator",
    page_icon="🎙️",
    layout="wide"
)

# ==================== DATABASE FUNCTIONS ====================

def init_database():
    """Initialize SQLite database with simple schema"""
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    
    # Conversations table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            fingerprint TEXT UNIQUE,
            filename TEXT,
            format TEXT,
            duration REAL,
            speakers INTEGER,
            turns INTEGER,
            processed_at TEXT,
            storage_path TEXT,
            last_viewed TEXT
        )
    """)
    
    # Migration: Add last_viewed column if it doesn't exist (for existing databases)
    try:
        cursor.execute("SELECT last_viewed FROM conversations LIMIT 1")
    except sqlite3.OperationalError:
        # Column doesn't exist, add it
        cursor.execute("ALTER TABLE conversations ADD COLUMN last_viewed TEXT")
        # Set initial value to processed_at for existing records
        cursor.execute("UPDATE conversations SET last_viewed = processed_at WHERE last_viewed IS NULL")
        conn.commit()
    
    # Turns table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS turns (
            id TEXT PRIMARY KEY,
            conversation_id TEXT,
            number INTEGER,
            speaker TEXT,
            text TEXT,
            start_ms INTEGER,
            end_ms INTEGER,
            audio_path TEXT,
            FOREIGN KEY (conversation_id) REFERENCES conversations(id)
        )
    """)
    
    # Index for fast fingerprint lookup
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_fingerprint 
        ON conversations(fingerprint)
    """)
    
    conn.commit()
    conn.close()

def get_audio_fingerprint(audio_bytes):
    """Generate SHA-256 fingerprint of audio content"""
    return hashlib.sha256(audio_bytes).hexdigest()

def find_existing_conversation(fingerprint):
    """Check if audio was already processed"""
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT id, filename, processed_at, turns, duration 
        FROM conversations 
        WHERE fingerprint = ?
    """, (fingerprint,))
    
    result = cursor.fetchone()
    conn.close()
    
    return result

def save_conversation(conv_id, fingerprint, filename, audio_format, 
                     duration, speakers, turns_count, storage_path):
    """Save conversation metadata to database"""
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    
    cursor.execute("""
        INSERT INTO conversations 
        (id, fingerprint, filename, format, duration, speakers, turns, processed_at, storage_path, last_viewed)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (conv_id, fingerprint, filename, audio_format, duration, 
          speakers, turns_count, datetime.now().isoformat(), storage_path, datetime.now().isoformat()))
    
    conn.commit()
    conn.close()

def update_last_viewed(conv_id):
    """Update last viewed timestamp for a conversation"""
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    
    cursor.execute("""
        UPDATE conversations 
        SET last_viewed = ? 
        WHERE id = ?
    """, (datetime.now().isoformat(), conv_id))
    
    conn.commit()
    conn.close()

def save_turn(turn_id, conv_id, number, speaker, text, start_ms, end_ms, audio_path):
    """Save individual turn to database"""
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    
    cursor.execute("""
        INSERT INTO turns 
        (id, conversation_id, number, speaker, text, start_ms, end_ms, audio_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (turn_id, conv_id, number, speaker, text, start_ms, end_ms, audio_path))
    
    conn.commit()
    conn.close()

def load_conversation_turns(conv_id):
    """Load all turns for a conversation"""
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT number, speaker, text, start_ms, end_ms, audio_path
        FROM turns 
        WHERE conversation_id = ?
        ORDER BY number
    """, (conv_id,))
    
    rows = cursor.fetchall()
    conn.close()
    
    turns = []
    for row in rows:
        # Load audio file
        audio_path = STORAGE_DIR / row[5]
        if audio_path.exists():
            with open(audio_path, 'rb') as f:
                audio_bytes = f.read()
            
            turns.append({
                'number': row[0],
                'speaker': row[1],
                'text': row[2],
                'start': row[3] / 1000,
                'end': row[4] / 1000,
                'audio_b64': base64.b64encode(audio_bytes).decode('utf-8')
            })
    
    return turns

def get_all_conversations():
    """Get list of all conversations with last viewed info"""
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT id, filename, processed_at, duration, turns, speakers, last_viewed
        FROM conversations 
        ORDER BY processed_at DESC
    """)
    
    rows = cursor.fetchall()
    conn.close()
    
    return rows

def get_conversation_by_id(conv_id):
    """Get conversation metadata by ID"""
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT id, filename, format, duration, turns, speakers, processed_at
        FROM conversations 
        WHERE id = ?
    """, (conv_id,))
    
    result = cursor.fetchone()
    conn.close()
    
    return result

def delete_conversation(conv_id):
    """Delete conversation and its files"""
    conn = sqlite3.connect(str(DB_PATH))
    cursor = conn.cursor()
    
    # Get storage path
    cursor.execute("SELECT storage_path FROM conversations WHERE id = ?", (conv_id,))
    result = cursor.fetchone()
    
    if result:
        # Delete files
        conv_dir = STORAGE_DIR / result[0]
        if conv_dir.exists():
            import shutil
            shutil.rmtree(conv_dir)
        
        # Delete from database
        cursor.execute("DELETE FROM turns WHERE conversation_id = ?", (conv_id,))
        cursor.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
        conn.commit()
    
    conn.close()

# ==================== STORAGE FUNCTIONS ====================

def save_audio_files(conv_id, audio_bytes, audio_format, turns_data):
    """Save original audio and segments to disk"""
    conv_dir = STORAGE_DIR / conv_id
    conv_dir.mkdir(parents=True, exist_ok=True)
    
    segments_dir = conv_dir / "segments"
    segments_dir.mkdir(exist_ok=True)
    
    # Save original audio
    original_path = conv_dir / f"original.{audio_format}"
    with open(original_path, 'wb') as f:
        f.write(audio_bytes)
    
    # Save segments
    for turn in turns_data:
        segment_filename = f"{turn['speaker'].lower()}_{turn['number']:03d}.{audio_format}"
        segment_path = segments_dir / segment_filename
        
        audio_bytes = base64.b64decode(turn['audio_b64'])
        with open(segment_path, 'wb') as f:
            f.write(audio_bytes)
        
        # Update turn data with relative path
        turn['audio_path'] = f"{conv_id}/segments/{segment_filename}"
    
    return conv_id

def get_mime_type(audio_format):
    """Get MIME type for audio format"""
    mime_types = {
        'wav': 'audio/wav',
        'mp3': 'audio/mpeg',
        'ogg': 'audio/ogg',
        'm4a': 'audio/mp4',
        'flac': 'audio/flac'
    }
    return mime_types.get(audio_format, 'audio/wav')

def create_zip_file(turns, audio_format):
    """Create ZIP file with all segments"""
    zip_buffer = io.BytesIO()
    
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for turn in turns:
            filename = f"{turn['speaker'].lower()}_{turn['number']:03d}.{audio_format}"
            audio_bytes = base64.b64decode(turn['audio_b64'])
            zip_file.writestr(filename, audio_bytes)
    
    zip_buffer.seek(0)
    return zip_buffer.getvalue()

def format_duration(seconds):
    """Format duration as MM:SS"""
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}:{secs:02d}"

def get_time_ago(iso_timestamp):
    """Get human-readable time ago string"""
    if not iso_timestamp:
        return "Never"
    
    try:
        dt = datetime.fromisoformat(iso_timestamp)
        now = datetime.now()
        diff = now - dt
        
        if diff.days > 0:
            return f"{diff.days}d ago"
        elif diff.seconds >= 3600:
            hours = diff.seconds // 3600
            return f"{hours}h ago"
        elif diff.seconds >= 60:
            minutes = diff.seconds // 60
            return f"{minutes}m ago"
        else:
            return "Just now"
    except:
        return "Unknown"

# ==================== PROCESSING FUNCTION ====================

def process_audio(audio_file_path, speakers_expected, audio_format):
    """Process audio with speaker separation"""
    progress = st.progress(0)
    status = st.empty()
    
    try:
        status.markdown("**🎤 Transcribing audio...**")
        progress.progress(30)
        
        config = aai.TranscriptionConfig(
            speaker_labels=True,
            speakers_expected=speakers_expected
        )
        
        transcript = aai.Transcriber().transcribe(audio_file_path, config=config)
        progress.progress(60)
        
        if transcript.status == aai.TranscriptStatus.error:
            st.error(f"⚠️ Error: {transcript.error}")
            return None
        
        status.markdown(f"**✂️ Extracting turns as {audio_format.upper()}...**")
        audio = AudioSegment.from_file(audio_file_path)
        
        turns = []
        for idx, utterance in enumerate(transcript.utterances, 1):
            start_ms = max(0, int(utterance.start))
            end_ms = min(len(audio), int(utterance.end))
            
            if end_ms <= start_ms:
                continue
            
            segment = audio[start_ms:end_ms]
            buffer = io.BytesIO()
            segment.export(buffer, format=audio_format)
            audio_bytes = buffer.getvalue()
            
            turns.append({
                'number': idx,
                'speaker': utterance.speaker,
                'text': utterance.text,
                'start': utterance.start / 1000,
                'end': utterance.end / 1000,
                'start_ms': start_ms,
                'end_ms': end_ms,
                'audio_b64': base64.b64encode(audio_bytes).decode('utf-8')
            })
        
        progress.progress(100)
        status.markdown("**✅ Complete!**")
        
        return turns
        
    except Exception as e:
        st.error(f"⚠️ Error: {str(e)}")
        return None

# ==================== UI STYLING ====================

st.markdown("""
    <style>
    /* ============================================
       DARK MODE & LIGHT MODE SUPPORT
       ============================================ */
    
    /* Detect dark mode using Streamlit's data-theme attribute */
    [data-theme="dark"] {
        --bg-primary: #1E1E1E;
        --bg-secondary: #2D2D2D;
        --bg-tertiary: #3A3A3A;
        --text-primary: #E0E0E0;
        --text-secondary: #B0B0B0;
        --text-tertiary: #808080;
        --border-color: #4A4A4A;
        --shadow-color: rgba(0, 0, 0, 0.5);
        --accent-primary: #4CAF50;
        --accent-secondary: #2196F3;
        --warning-bg: #332800;
        --warning-border: #FF9800;
        --error-bg: #2C1515;
        --error-border: #EF5350;
        --success-bg: #1B3A1B;
        --success-border: #4CAF50;
        --bubble-a-bg: #2C5F2D;
        --bubble-b-bg: #2D2D2D;
        --card-hover: #3D3D3D;
    }
    
    [data-theme="light"], :root {
        --bg-primary: #FFFFFF;
        --bg-secondary: #F5F5F5;
        --bg-tertiary: #E8E8E8;
        --text-primary: #1A1A1A;
        --text-secondary: #4A4A4A;
        --text-tertiary: #767676;
        --border-color: #D0D0D0;
        --shadow-color: rgba(0, 0, 0, 0.1);
        --accent-primary: #075E54;
        --accent-secondary: #128C7E;
        --warning-bg: #FFF3E0;
        --warning-border: #FF9800;
        --error-bg: #FFEBEE;
        --error-border: #EF5350;
        --success-bg: #E8F5E9;
        --success-border: #4CAF50;
        --bubble-a-bg: #DCF8C6;
        --bubble-b-bg: #FFFFFF;
        --card-hover: #F0F0F0;
    }
    
    /* ============================================
       RESPONSIVE BREAKPOINTS
       ============================================ */
    
    /* Mobile: < 768px */
    @media (max-width: 767px) {
        .chat-header {
            flex-direction: column;
            gap: 8px;
            text-align: center;
        }
        
        .message-count {
            width: 100%;
            text-align: center;
        }
        
        .history-card, .history-card-active, .history-card-recent {
            padding: 12px !important;
        }
        
        .history-title {
            font-size: 14px !important;
        }
        
        .history-meta {
            font-size: 11px !important;
        }
        
        .badge {
            font-size: 10px !important;
            padding: 2px 6px !important;
        }
    }
    
    /* Tablet: 768px - 1024px */
    @media (min-width: 768px) and (max-width: 1024px) {
        .chat-background {
            padding: 15px !important;
        }
        
        .message-bubble-a, .message-bubble-b {
            max-width: 85%;
        }
    }
    
    /* Desktop: > 1024px */
    @media (min-width: 1025px) {
        .message-bubble-a, .message-bubble-b {
            max-width: 70%;
        }
    }
    
    /* ============================================
       MESSAGE BUBBLES (WhatsApp Style)
       ============================================ */
    
    .message-bubble-a {
        background: var(--bubble-a-bg);
        padding: 12px 14px;
        border-radius: 12px;
        border-bottom-right-radius: 3px;
        box-shadow: 0 1px 3px var(--shadow-color);
        margin: 8px 0;
        transition: transform 0.2s ease, box-shadow 0.2s ease;
    }
    
    .message-bubble-a:hover {
        transform: translateY(-1px);
        box-shadow: 0 2px 6px var(--shadow-color);
    }
    
    .message-bubble-b {
        background: var(--bubble-b-bg);
        padding: 12px 14px;
        border-radius: 12px;
        border-bottom-left-radius: 3px;
        box-shadow: 0 1px 3px var(--shadow-color);
        margin: 8px 0;
        border: 1px solid var(--border-color);
        transition: transform 0.2s ease, box-shadow 0.2s ease;
    }
    
    .message-bubble-b:hover {
        transform: translateY(-1px);
        box-shadow: 0 2px 6px var(--shadow-color);
    }
    
    .speaker-name {
        font-weight: 700;
        font-size: 13px;
        color: var(--accent-primary);
        margin-bottom: 6px;
        letter-spacing: 0.3px;
    }
    
    .message-text {
        font-size: 14px;
        line-height: 1.6;
        color: var(--text-primary);
        margin: 6px 0 8px 0;
        word-wrap: break-word;
    }
    
    .message-time {
        font-size: 11px;
        color: var(--text-tertiary);
        margin-bottom: 8px;
        opacity: 0.8;
    }
    
    /* ============================================
       CHAT INTERFACE
       ============================================ */
    
    .chat-header {
        background: linear-gradient(135deg, var(--accent-primary) 0%, var(--accent-secondary) 100%);
        color: white;
        padding: 16px 20px;
        border-radius: 12px 12px 0 0;
        font-size: 17px;
        font-weight: 600;
        display: flex;
        justify-content: space-between;
        align-items: center;
        box-shadow: 0 2px 8px var(--shadow-color);
    }
    
    .message-count {
        background: rgba(255, 255, 255, 0.95);
        color: var(--accent-primary);
        padding: 6px 14px;
        border-radius: 20px;
        font-size: 13px;
        font-weight: 600;
        box-shadow: 0 2px 4px rgba(0, 0, 0, 0.2);
    }
    
    .chat-background {
        background: var(--bg-secondary);
        padding: 20px;
        border-radius: 0 0 12px 12px;
        border: 1px solid var(--border-color);
        border-top: none;
    }
    
    /* ============================================
       HISTORY CARDS
       ============================================ */
    
    .history-card {
        background: var(--bg-primary);
        padding: 16px;
        border-radius: 10px;
        border-left: 4px solid var(--accent-primary);
        margin: 12px 0;
        box-shadow: 0 2px 8px var(--shadow-color);
        transition: all 0.3s ease;
        border: 1px solid var(--border-color);
    }
    
    .history-card:hover {
        transform: translateX(4px);
        box-shadow: 0 4px 12px var(--shadow-color);
        background: var(--card-hover);
    }
    
    .history-card-active {
        background: var(--success-bg);
        padding: 16px;
        border-radius: 10px;
        border-left: 4px solid var(--success-border);
        margin: 12px 0;
        box-shadow: 0 3px 12px rgba(76, 175, 80, 0.3);
        transition: all 0.3s ease;
        border: 1px solid var(--success-border);
    }
    
    .history-card-active:hover {
        transform: translateX(4px);
        box-shadow: 0 5px 16px rgba(76, 175, 80, 0.4);
    }
    
    .history-card-recent {
        background: var(--warning-bg);
        padding: 16px;
        border-radius: 10px;
        border-left: 4px solid var(--warning-border);
        margin: 12px 0;
        box-shadow: 0 2px 10px rgba(255, 152, 0, 0.2);
        transition: all 0.3s ease;
        border: 1px solid var(--warning-border);
    }
    
    .history-card-recent:hover {
        transform: translateX(4px);
        box-shadow: 0 4px 14px rgba(255, 152, 0, 0.3);
    }
    
    .history-title {
        font-size: 16px;
        font-weight: 600;
        color: var(--text-primary);
        margin-bottom: 8px;
        display: flex;
        align-items: center;
        flex-wrap: wrap;
        gap: 8px;
    }
    
    .history-meta {
        font-size: 12px;
        color: var(--text-secondary);
        line-height: 1.6;
    }
    
    /* ============================================
       BADGES
       ============================================ */
    
    .badge {
        display: inline-block;
        padding: 4px 10px;
        border-radius: 14px;
        font-size: 11px;
        font-weight: 700;
        letter-spacing: 0.5px;
        text-transform: uppercase;
        margin-left: 8px;
        box-shadow: 0 2px 4px var(--shadow-color);
    }
    
    .badge-active {
        background: linear-gradient(135deg, #4CAF50 0%, #388E3C 100%);
        color: white;
    }
    
    .badge-recent {
        background: linear-gradient(135deg, #FF9800 0%, #F57C00 100%);
        color: white;
    }
    
    .badge-duplicate {
        background: linear-gradient(135deg, #EF5350 0%, #D32F2F 100%);
        color: white;
    }
    
    /* ============================================
       NOTIFICATION BOXES
       ============================================ */
    
    .duplicate-warning {
        background: var(--error-bg);
        border: 2px solid var(--error-border);
        border-radius: 10px;
        padding: 20px;
        margin: 15px 0;
        box-shadow: 0 4px 12px var(--shadow-color);
    }
    
    .duplicate-warning h2 {
        color: var(--error-border) !important;
        margin: 0 0 12px 0 !important;
        font-size: 1.4em !important;
    }
    
    .duplicate-warning p {
        color: var(--text-primary) !important;
        line-height: 1.6 !important;
    }
    
    .duplicate-info {
        background: var(--bg-secondary);
        border-left: 4px solid var(--accent-secondary);
        padding: 16px;
        border-radius: 6px;
        margin: 12px 0;
        box-shadow: 0 2px 6px var(--shadow-color);
        border: 1px solid var(--border-color);
    }
    
    .duplicate-info p {
        margin: 6px 0 !important;
        color: var(--text-primary) !important;
        line-height: 1.6 !important;
    }
    
    .duplicate-info strong {
        color: var(--accent-secondary) !important;
        font-weight: 600 !important;
    }
    
    .duplicate-info code {
        background: var(--bg-tertiary);
        padding: 2px 6px;
        border-radius: 4px;
        font-family: 'Courier New', monospace;
        font-size: 0.9em;
        color: var(--text-primary);
    }
    
    .prevent-message {
        background: var(--warning-bg);
        border: 2px solid var(--warning-border);
        border-radius: 10px;
        padding: 18px;
        margin: 12px 0;
        font-weight: 500;
        box-shadow: 0 3px 10px var(--shadow-color);
    }
    
    .prevent-message h3 {
        color: var(--warning-border) !important;
        margin: 0 0 10px 0 !important;
    }
    
    .prevent-message p, .prevent-message ul, .prevent-message li {
        color: var(--text-primary) !important;
        line-height: 1.6 !important;
    }
    
    /* ============================================
       BANNER (Currently Viewing)
       ============================================ */
    
    .viewing-banner {
        background: linear-gradient(135deg, var(--success-bg) 0%, var(--bg-secondary) 100%);
        padding: 16px 20px;
        border-radius: 10px;
        margin-bottom: 16px;
        border-left: 5px solid var(--success-border);
        box-shadow: 0 3px 10px var(--shadow-color);
        border: 1px solid var(--border-color);
    }
    
    .viewing-banner h4 {
        margin: 0 0 10px 0 !important;
        color: var(--accent-primary) !important;
        font-size: 1.1em !important;
    }
    
    .viewing-banner p {
        margin: 5px 0 !important;
        font-size: 14px !important;
        color: var(--text-secondary) !important;
    }
    
    /* ============================================
       LEGEND BOX
       ============================================ */
    
    .legend-box {
        background: var(--bg-secondary);
        padding: 12px 16px;
        border-radius: 8px;
        margin-bottom: 16px;
        border: 1px solid var(--border-color);
        box-shadow: 0 2px 6px var(--shadow-color);
    }
    
    .legend-box strong {
        color: var(--text-primary) !important;
        margin-right: 10px;
    }
    
    /* ============================================
       BUTTONS (Streamlit Override)
       ============================================ */
    
    .stButton > button {
        border-radius: 8px !important;
        font-weight: 500 !important;
        transition: all 0.3s ease !important;
        border: 1px solid transparent !important;
    }
    
    .stButton > button:hover {
        transform: translateY(-2px) !important;
        box-shadow: 0 4px 12px var(--shadow-color) !important;
    }
    
    .stDownloadButton > button {
        border-radius: 6px !important;
        transition: all 0.2s ease !important;
    }
    
    .stDownloadButton > button:hover {
        transform: translateY(-1px) !important;
    }
    
    /* ============================================
       AUDIO PLAYER
       ============================================ */
    
    audio {
        width: 100%;
        margin: 8px 0;
        border-radius: 8px;
        filter: contrast(1.1);
    }
    
    /* ============================================
       SCROLLBAR STYLING
       ============================================ */
    
    div[data-testid="stVerticalBlock"] > div[style*="overflow"] {
        scrollbar-width: thin;
        scrollbar-color: var(--accent-primary) var(--bg-tertiary);
    }
    
    div[data-testid="stVerticalBlock"] > div[style*="overflow"]::-webkit-scrollbar {
        width: 10px;
    }
    
    div[data-testid="stVerticalBlock"] > div[style*="overflow"]::-webkit-scrollbar-track {
        background: var(--bg-tertiary);
        border-radius: 10px;
    }
    
    div[data-testid="stVerticalBlock"] > div[style*="overflow"]::-webkit-scrollbar-thumb {
        background: var(--accent-primary);
        border-radius: 10px;
        border: 2px solid var(--bg-tertiary);
    }
    
    div[data-testid="stVerticalBlock"] > div[style*="overflow"]::-webkit-scrollbar-thumb:hover {
        background: var(--accent-secondary);
    }
    
    /* ============================================
       ANIMATIONS
       ============================================ */
    
    @keyframes fadeIn {
        from {
            opacity: 0;
            transform: translateY(10px);
        }
        to {
            opacity: 1;
            transform: translateY(0);
        }
    }
    
    .history-card, .history-card-active, .history-card-recent {
        animation: fadeIn 0.3s ease;
    }
    
    .message-bubble-a, .message-bubble-b {
        animation: fadeIn 0.2s ease;
    }
    
    /* ============================================
       ACCESSIBILITY
       ============================================ */
    
    @media (prefers-reduced-motion: reduce) {
        * {
            animation: none !important;
            transition: none !important;
        }
    }
    
    /* High contrast mode support */
    @media (prefers-contrast: high) {
        .message-bubble-a, .message-bubble-b,
        .history-card, .history-card-active, .history-card-recent {
            border: 2px solid currentColor !important;
        }
    }
    
    /* ============================================
       PRINT STYLES
       ============================================ */
    
    @media print {
        .stButton, .stDownloadButton, audio {
            display: none !important;
        }
        
        .message-bubble-a, .message-bubble-b {
            box-shadow: none !important;
            border: 1px solid #000 !important;
        }
    }
    </style>
""", unsafe_allow_html=True)

# ==================== INITIALIZE ====================

init_database()

# Initialize session state
if 'page' not in st.session_state:
    st.session_state.page = 'upload'
if 'current_turns' not in st.session_state:
    st.session_state.current_turns = None
if 'current_filename' not in st.session_state:
    st.session_state.current_filename = None
if 'current_format' not in st.session_state:
    st.session_state.current_format = None
if 'current_conversation_id' not in st.session_state:
    st.session_state.current_conversation_id = None
if 'uploaded_fingerprint' not in st.session_state:
    st.session_state.uploaded_fingerprint = None
if 'show_override_warning' not in st.session_state:
    st.session_state.show_override_warning = False

# ==================== SIDEBAR NAVIGATION ====================

with st.sidebar:
    st.markdown("### 📂 Navigation")
    
    if st.button("📤 Upload New Audio", use_container_width=True):
        st.session_state.page = 'upload'
        st.session_state.current_turns = None
        st.session_state.current_conversation_id = None
        st.session_state.uploaded_fingerprint = None
        st.session_state.show_override_warning = False
        st.rerun()
    
    if st.button("📜 View History", use_container_width=True):
        st.session_state.page = 'history'
        st.session_state.current_turns = None
        st.session_state.uploaded_fingerprint = None
        st.session_state.show_override_warning = False
        st.rerun()
    
    st.markdown("---")
    
    if st.session_state.page == 'upload':
        st.markdown("### ⚙️ Configuration")
        speakers_expected = st.number_input(
            "Expected Speakers",
            min_value=2,
            max_value=10,
            value=2
        )
        
        st.markdown("---")
        st.markdown("### 📋 Steps")
        st.markdown("""
        1. Upload audio file
        2. ✨ Auto-check duplicates
        3. 🚫 Prevent reprocessing
        4. 💾 View saved results
        """)
    else:
        # Show storage stats
        conversations = get_all_conversations()
        st.markdown(f"### 📊 Statistics")
        st.metric("Total Conversations", len(conversations))
        
        # Calculate total storage
        total_size = 0
        for conv in conversations:
            conv_id = conv[0]
            conv_dir = STORAGE_DIR / conv_id
            if conv_dir.exists():
                for file_path in conv_dir.rglob('*'):
                    if file_path.is_file():
                        total_size += file_path.stat().st_size
        
        size_mb = total_size / (1024 * 1024)
        st.metric("Storage Used", f"{size_mb:.1f} MB")

# ==================== MAIN PAGES ====================

if st.session_state.page == 'upload':
    st.title("🎙️ Audio Speaker Separator")
    st.markdown("Upload an audio file to separate speakers and view as WhatsApp-style chat")
    
    uploaded_file = st.file_uploader(
        "Choose an audio file",
        type=['wav', 'mp3', 'm4a', 'flac', 'ogg']
    )
    
    if uploaded_file:
        st.success(f"✅ File uploaded: **{uploaded_file.name}**")
        
        # Get file info
        audio_bytes = uploaded_file.getvalue()
        file_ext = Path(uploaded_file.name).suffix.lower().replace('.', '')
        fingerprint = get_audio_fingerprint(audio_bytes)
        
        # Store fingerprint in session
        st.session_state.uploaded_fingerprint = fingerprint
        
        # Check for duplicate
        existing = find_existing_conversation(fingerprint)
        
        if existing and not st.session_state.show_override_warning:
            conv_id, filename, processed_at, turns_count, duration = existing
            
            # Show prominent duplicate warning
            st.markdown("""
                <div class='duplicate-warning'>
                    <h2 style='color: #D32F2F; margin: 0 0 10px 0;'>
                        🚫 DUPLICATE DETECTED - REPROCESSING BLOCKED
                    </h2>
                    <p style='font-size: 16px; margin: 10px 0;'>
                        This audio file has already been processed and saved to your database.
                    </p>
                </div>
            """, unsafe_allow_html=True)
            
            # Show existing conversation info
            time_ago = get_time_ago(processed_at)
            st.markdown(f"""
                <div class='duplicate-info'>
                    <p style='margin: 5px 0;'><strong>📁 Original Filename:</strong> {filename}</p>
                    <p style='margin: 5px 0;'><strong>📅 Processed:</strong> {processed_at[:10]} ({time_ago})</p>
                    <p style='margin: 5px 0;'><strong>💬 Turns:</strong> {turns_count}</p>
                    <p style='margin: 5px 0;'><strong>⏱️ Duration:</strong> {format_duration(duration)}</p>
                    <p style='margin: 5px 0;'><strong>🔐 Fingerprint:</strong> <code>{fingerprint[:16]}...</code></p>
                </div>
            """, unsafe_allow_html=True)
            
            st.info("ℹ️ **Why can't I reprocess?** To save time, API calls, and prevent duplicate data, the system automatically loads your previously processed results.")
            
            col1, col2, col3 = st.columns([2, 2, 1])
            
            with col1:
                if st.button("📂 Load Saved Results", type="primary", use_container_width=True):
                    turns = load_conversation_turns(conv_id)
                    update_last_viewed(conv_id)
                    st.session_state.current_turns = turns
                    st.session_state.current_filename = Path(filename).stem
                    st.session_state.current_format = file_ext
                    st.session_state.current_conversation_id = conv_id
                    st.success("✅ Loaded from database instantly!")
                    st.rerun()
            
            with col2:
                if st.button("📜 View in History", use_container_width=True):
                    st.session_state.page = 'history'
                    st.session_state.current_conversation_id = conv_id
                    st.rerun()
            
            with col3:
                if st.button("⚠️ Override", use_container_width=True):
                    st.session_state.show_override_warning = True
                    st.rerun()
        
        elif st.session_state.show_override_warning:
            # Show override confirmation
            st.markdown("""
                <div class='prevent-message'>
                    <h3 style='color: #E65100; margin: 0 0 10px 0;'>⚠️ Override Confirmation Required</h3>
                    <p>You are about to reprocess audio that already exists in your database.</p>
                    <p><strong>This will:</strong></p>
                    <ul>
                        <li>Use additional API credits</li>
                        <li>Take 1-2 minutes to process</li>
                        <li>Create a duplicate entry in your database</li>
                        <li>Waste storage space</li>
                    </ul>
                    <p><strong>Are you absolutely sure?</strong></p>
                </div>
            """, unsafe_allow_html=True)
            
            col1, col2 = st.columns(2)
            
            with col1:
                if st.button("❌ Cancel - Load Saved Results", type="primary", use_container_width=True):
                    existing = find_existing_conversation(st.session_state.uploaded_fingerprint)
                    if existing:
                        conv_id, filename, _, _, _ = existing
                        turns = load_conversation_turns(conv_id)
                        update_last_viewed(conv_id)
                        st.session_state.current_turns = turns
                        st.session_state.current_filename = Path(filename).stem
                        st.session_state.current_format = file_ext
                        st.session_state.current_conversation_id = conv_id
                        st.session_state.show_override_warning = False
                        st.rerun()
            
            with col2:
                if st.button("✅ Yes, Process Anyway", use_container_width=True):
                    # Allow processing
                    pass  # Continue to processing section below
        
        # Process button - only shown if no duplicate OR override confirmed
        if not existing or st.session_state.show_override_warning:
            # Only enable button if: no duplicate OR override warning is showing
            button_enabled = (not existing) or st.session_state.show_override_warning
            
            if st.button("🚀 Process Audio", type="primary", use_container_width=True, disabled=(not button_enabled)):
                # Create temp file
                with tempfile.NamedTemporaryFile(delete=False, suffix=f'.{file_ext}') as tmp:
                    tmp.write(audio_bytes)
                    tmp_path = tmp.name
                
                # Handle OGG conversion
                process_path = tmp_path
                if file_ext == 'ogg':
                    st.info("🔄 Converting OGG to WAV...")
                    audio = AudioSegment.from_ogg(tmp_path)
                    wav_path = tmp_path.replace('.ogg', '.wav')
                    audio.export(wav_path, format="wav")
                    process_path = wav_path
                
                # Process
                turns = process_audio(process_path, speakers_expected, file_ext)
                
                # Cleanup temp files
                os.unlink(tmp_path)
                if process_path != tmp_path:
                    os.unlink(process_path)
                
                if turns:
                    # Generate IDs
                    conv_id = str(uuid.uuid4())
                    
                    # Calculate duration
                    duration = turns[-1]['end'] if turns else 0
                    
                    # Save files
                    save_audio_files(conv_id, audio_bytes, file_ext, turns)
                    
                    # Save to database
                    save_conversation(
                        conv_id, fingerprint, uploaded_file.name, 
                        file_ext, duration, speakers_expected, 
                        len(turns), conv_id
                    )
                    
                    # Save turns
                    for turn in turns:
                        turn_id = str(uuid.uuid4())
                        save_turn(
                            turn_id, conv_id, turn['number'], 
                            turn['speaker'], turn['text'],
                            turn['start_ms'], turn['end_ms'],
                            turn['audio_path']
                        )
                    
                    st.session_state.current_turns = turns
                    st.session_state.current_filename = Path(uploaded_file.name).stem
                    st.session_state.current_format = file_ext
                    st.session_state.current_conversation_id = conv_id
                    st.session_state.show_override_warning = False
                    st.success("💾 Conversation saved to database!")
                    st.rerun()

elif st.session_state.page == 'history':
    st.title("📜 Conversation History")
    
    conversations = get_all_conversations()
    
    if not conversations:
        st.info("📭 No conversations yet. Upload an audio file to get started!")
    else:
        st.markdown(f"**Found {len(conversations)} conversation(s)**")
        st.markdown("---")
        
        for conv in conversations:
            conv_id, filename, processed_at, duration, turns, speakers, last_viewed = conv
            
            # Determine card style based on status
            is_current = (st.session_state.current_conversation_id == conv_id)
            
            # Check if recently viewed (last 5 minutes)
            is_recent = False
            if last_viewed:
                try:
                    viewed_dt = datetime.fromisoformat(last_viewed)
                    now = datetime.now()
                    diff = (now - viewed_dt).total_seconds()
                    is_recent = diff < 300  # 5 minutes
                except:
                    pass
            
            # Choose card style
            if is_current:
                card_class = 'history-card-active'
                badge_html = "<span class='badge badge-active'>VIEWING</span>"
            elif is_recent:
                card_class = 'history-card-recent'
                badge_html = "<span class='badge badge-recent'>RECENT</span>"
            else:
                card_class = 'history-card'
                badge_html = ""
            
            time_ago = get_time_ago(last_viewed)
            
            # Create container for each conversation
            with st.container():
                st.markdown(f"""
                    <div class='{card_class}'>
                        <div class='history-title'>
                            🎵 {filename} {badge_html}
                        </div>
                        <div class='history-meta'>
                            📅 Processed: {processed_at[:10]} | 
                            👁️ Last viewed: {time_ago} | 
                            ⏱️ {format_duration(duration)} | 
                            💬 {turns} turns | 
                            👥 {speakers} speakers
                        </div>
                    </div>
                """, unsafe_allow_html=True)
                
                # Action buttons below the card
                col1, col2, col3 = st.columns([2, 1, 1])
                
                with col1:
                    if st.button("👁️ View", key=f"view_{conv_id}", use_container_width=True, type="primary" if is_current else "secondary"):
                        turns = load_conversation_turns(conv_id)
                        update_last_viewed(conv_id)
                        
                        # Get conversation metadata for format
                        conv_meta = get_conversation_by_id(conv_id)
                        if conv_meta:
                            _, filename, audio_format, _, _, _, _ = conv_meta
                            st.session_state.current_turns = turns
                            st.session_state.current_filename = Path(filename).stem
                            st.session_state.current_format = audio_format
                            st.session_state.current_conversation_id = conv_id
                            st.rerun()
                
                with col2:
                    # Download original
                    original_path = STORAGE_DIR / conv_id / f"original.{filename.split('.')[-1]}"
                    if original_path.exists():
                        with open(original_path, 'rb') as f:
                            st.download_button(
                                "⬇️ Audio",
                                f.read(),
                                filename,
                                key=f"dl_{conv_id}",
                                use_container_width=True
                            )
                
                with col3:
                    if st.button("🗑️ Delete", key=f"del_{conv_id}", use_container_width=True):
                        delete_conversation(conv_id)
                        
                        # Clear current if deleting active conversation
                        if st.session_state.current_conversation_id == conv_id:
                            st.session_state.current_turns = None
                            st.session_state.current_conversation_id = None
                        
                        st.success("✅ Deleted!")
                        st.rerun()
                
                # Add spacing between cards
                st.markdown("<br>", unsafe_allow_html=True)

# ==================== DISPLAY RESULTS ====================

if st.session_state.current_turns:
    turns = st.session_state.current_turns
    filename = st.session_state.current_filename
    audio_format = st.session_state.current_format
    mime_type = get_mime_type(audio_format)
    
    st.markdown("---")
    
    # Show conversation info banner
    if st.session_state.current_conversation_id:
        conv_meta = get_conversation_by_id(st.session_state.current_conversation_id)
        if conv_meta:
            _, orig_filename, _, duration, turn_count, speaker_count, processed_date = conv_meta
            st.markdown(f"""
                <div class='viewing-banner'>
                    <h4>📂 Currently Viewing: {orig_filename}</h4>
                    <p>
                        📅 Processed: {processed_date[:10]} | 
                        ⏱️ Duration: {format_duration(duration)} | 
                        💬 {turn_count} turns | 
                        👥 {speaker_count} speakers
                    </p>
                </div>
            """, unsafe_allow_html=True)
    
    st.info(f"📊 Format: **{audio_format.upper()}** | Segments: **{len(turns)}**")
    
    # Download all button
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        zip_data = create_zip_file(turns, audio_format)
        st.download_button(
            "📦 Download All Segments (ZIP)",
            zip_data,
            f"{filename}_all_segments.zip",
            "application/zip",
            use_container_width=True
        )
    
    st.markdown("<br>", unsafe_allow_html=True)
    
    # Chat display
    st.markdown(f"""
        <div class='chat-header'>
            <span>💬 Conversation Transcript</span>
            <span class='message-count'>{len(turns)} messages</span>
        </div>
        <div class='chat-background'>
    """, unsafe_allow_html=True)
    
    chat_container = st.container(height=550)
    
    with chat_container:
        for turn in turns:
            audio_bytes = base64.b64decode(turn['audio_b64'])
            
            if turn['speaker'].lower() == 'a':
                col1, col2 = st.columns([0.3, 0.7])
                with col2:
                    st.markdown(f"""
                        <div class='message-bubble-a'>
                            <div class='speaker-name'>{turn['speaker'].upper()}</div>
                            <div class='message-text'>{turn['text']}</div>
                            <div class='message-time'>⏱️ {turn['start']:.1f}s - {turn['end']:.1f}s</div>
                        </div>
                    """, unsafe_allow_html=True)
                    st.audio(audio_bytes, format=mime_type)
                    st.download_button(
                        f"⬇️ Download Turn {turn['number']}",
                        audio_bytes,
                        f"{turn['speaker']}_{turn['number']:03d}.{audio_format}",
                        mime_type,
                        key=f"dl_{turn['number']}"
                    )
            else:
                col1, col2 = st.columns([0.7, 0.3])
                with col1:
                    st.markdown(f"""
                        <div class='message-bubble-b'>
                            <div class='speaker-name'>{turn['speaker'].upper()}</div>
                            <div class='message-text'>{turn['text']}</div>
                            <div class='message-time'>⏱️ {turn['start']:.1f}s - {turn['end']:.1f}s</div>
                        </div>
                    """, unsafe_allow_html=True)
                    st.audio(audio_bytes, format=mime_type)
                    st.download_button(
                        f"⬇️ Download Turn {turn['number']}",
                        audio_bytes,
                        f"{turn['speaker']}_{turn['number']:03d}.{audio_format}",
                        mime_type,
                        key=f"dl_b_{turn['number']}"
                    )
    
    st.markdown("</div>", unsafe_allow_html=True)

# Footer
st.markdown("---")
st.markdown("<p style='text-align: center; color: #888;'>Powered by Streamlit & AssemblyAI</p>", unsafe_allow_html=True)