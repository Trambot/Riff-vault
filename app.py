import os
import io
import urllib.parse
import requests
from flask import Flask, render_template, request, redirect, url_for, send_file
from flask_sqlalchemy import SQLAlchemy
import cloudinary
import cloudinary.uploader
import cloudinary.api
import cloudinary.utils
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

app = Flask(__name__)
# Uses a local SQLite DB. Note: On free hosting, play counts reset if the server sleeps, 
# but the songs are permanently safe in Cloudinary.
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///riff_vault.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# --- CLOUDINARY CONFIGURATION ---
# We use environment variables so your keys stay hidden and secure when deployed online
cloudinary.config(
    cloud_name = os.environ.get("CLOUDINARY_CLOUD_NAME"),
    api_key = os.environ.get("CLOUDINARY_API_KEY"),
    api_secret = os.environ.get("CLOUDINARY_API_SECRET"),
    secure = True
)

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"]
)

db = SQLAlchemy(app)

# --- DATABASE SCHEMA ---
playlist_tracks = db.Table('playlist_tracks',
    db.Column('playlist_id', db.Integer, db.ForeignKey('playlist.id'), primary_key=True),
    db.Column('track_id', db.Integer, db.ForeignKey('track.id'), primary_key=True)
)

class Track(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(300), unique=True, nullable=False)
    plays = db.Column(db.Integer, default=0)
    rating = db.Column(db.Integer, default=0)
    bitrate = db.Column(db.String(50), default="Cloud Stream")
    sample_rate = db.Column(db.String(50), default="Auto")

class Playlist(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    is_auto = db.Column(db.Boolean, default=False) 
    tracks = db.relationship('Track', secondary=playlist_tracks, lazy='subquery', backref=db.backref('playlists', lazy=True))

class SongRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)

def sync_library():
    print("☁️ Running deep scan across Cloudinary...")
    try:
        total_found = 0
        # Scan video/audio, raw, and image asset types
        for r_type in ["video", "raw", "image"]:
            response = cloudinary.api.resources(resource_type=r_type, max_results=500)
            resources = response.get('resources', [])
            print(f"📦 Type '{r_type}': Found {len(resources)} items.")
            
            for res in resources:
                public_id = res.get('public_id') 
                if not public_id:
                    continue

                # Skip cover art files so they don't show up as playable songs in the UI
                if 'cover' in public_id.lower():
                    continue

                total_found += 1
                parts = public_id.split('/')
                folder_name = parts[0] if len(parts) > 1 else "Root"
                
                # 1. Handle Playlist
                playlist = db.session.query(Playlist).filter_by(name=folder_name).first()
                if not playlist:
                    playlist = Playlist(name=folder_name, is_auto=True)
                    db.session.add(playlist)
                    db.session.commit()
                    
                # 2. Handle Track
                track = db.session.query(Track).filter_by(filename=public_id).first()
                if not track:
                    track = Track(
                        filename=public_id,
                        plays=0, 
                        rating=0, 
                        bitrate="Cloud Stream", 
                        sample_rate="Auto"
                    )
                    db.session.add(track)
                    db.session.commit()
                    
                # 3. Link Track
                if track not in playlist.tracks:
                    playlist.tracks.append(track)
                    db.session.commit()
                    
        print(f"✅ Sync complete! Added {total_found} total tracks to database.")
    except Exception as e:
        print(f"❌ Cloudinary sync failed: {e}")
# Build DB and Sync before the app starts handling requests (Crucial for Gunicorn/Render)
with app.app_context():
    db.create_all()
    sync_library()

# --- ROUTES ---
@app.route("/")
def home():
    all_playlists = db.session.query(Playlist).all()
    song_requests = db.session.query(SongRequest).all()
    search_query = request.args.get("q")
    playlist_id = request.args.get("playlist_id")
    current_playlist = None
    
    if playlist_id:
        current_playlist = db.session.get(Playlist, playlist_id)
        db_tracks = current_playlist.tracks if current_playlist else []
    elif search_query:
        db_tracks = db.session.query(Track).filter(Track.filename.ilike(f"%{search_query}%")).all()
    else:
        db_tracks = db.session.query(Track).all()
        
    return render_template("home.html", app_name="Riff Vault", tracks=db_tracks, playlists=all_playlists, current_playlist=current_playlist, song_requests=song_requests)

@app.route("/upload", methods=["POST"])
def upload_track():
    if 'audio_file' not in request.files:
        return redirect(url_for('home'))
        
    file = request.files['audio_file']
    folder_name = request.form.get('folder_name', 'Root').strip() or 'Root'
    
    if file.filename != '':
        try:
            cloudinary.uploader.upload(file, resource_type="video", folder=folder_name, use_filename=True, unique_filename=False)
            sync_library() # Re-sync to grab the new song immediately
        except Exception as e:
            print(f"Upload failed: {e}")
            
    return redirect(url_for("home"))

@app.route("/create_playlist", methods=["POST"])
def create_playlist():
    name = request.form.get("playlist_name")
    if name and not db.session.query(Playlist).filter_by(name=name).first():
        db.session.add(Playlist(name=name, is_auto=False))
        db.session.commit()
    return redirect(url_for("home"))

@app.route("/delete_playlist/<int:pl_id>", methods=["POST"])
def delete_playlist(pl_id):
    pl = db.session.get(Playlist, pl_id)
    if pl and not pl.is_auto: 
        db.session.delete(pl)
        db.session.commit()
    return redirect(url_for("home"))

@app.route("/add_to_playlist", methods=["POST"])
def add_to_playlist():
    track_id, playlist_id = request.form.get("track_id"), request.form.get("playlist_id")
    if track_id and playlist_id:
        track, playlist = db.session.get(Track, track_id), db.session.get(Playlist, playlist_id)
        if track and playlist and not playlist.is_auto and track not in playlist.tracks:
            playlist.tracks.append(track)
            db.session.commit()
    return redirect(request.referrer or url_for("home"))

@app.route("/remove_from_playlist", methods=["POST"])
def remove_from_playlist():
    track_id, playlist_id = request.form.get("track_id"), request.form.get("playlist_id")
    if track_id and playlist_id:
        track, playlist = db.session.get(Track, track_id), db.session.get(Playlist, playlist_id)
        if track and playlist and not playlist.is_auto and track in playlist.tracks:
            playlist.tracks.remove(track)
            db.session.commit()
    return redirect(request.referrer or url_for("home"))

@app.route("/request_song", methods=["POST"])
@limiter.limit("3 per minute") 
def request_song():
    title = request.form.get("title")
    if title:
        db.session.add(SongRequest(title=title))
        db.session.commit()
    return redirect(url_for("home"))

@app.route("/clear_request/<int:req_id>", methods=["POST"])
def clear_request(req_id):
    req = db.session.get(SongRequest, req_id)
    if req:
        db.session.delete(req)
        db.session.commit()
    return redirect(url_for("home"))

@app.route("/add_play/<int:track_id>", methods=["POST"])
def add_play(track_id):
    track = db.session.get(Track, track_id)
    if track:
        track.plays += 1
        db.session.commit()
    return "", 204

@app.route("/stream/<path:filename>")
@limiter.exempt
def stream_audio(filename):
    clean_filename = urllib.parse.unquote(filename)
    optimized_url, _ = cloudinary.utils.cloudinary_url(clean_filename, resource_type="video")
    return redirect(optimized_url)

@app.route("/cover/<path:filename>")
@limiter.exempt
def get_cover(filename):
    # Auto-fetch high-res cover art from iTunes! No manual uploads needed.
    clean_title = urllib.parse.unquote(filename).split('/')[-1]
    clean_title = clean_title.replace('_', ' ').replace('.flac', '').replace('.mp3', '')
    
    try:
        url = f"https://itunes.apple.com/search?term={clean_title}&media=music&limit=1"
        response = requests.get(url, timeout=3)
        data = response.json()
        if data.get('results'):
            cover_url = data['results'][0]['artworkUrl100'].replace('100x100bb', '500x500bb')
            return redirect(cover_url)
    except Exception:
        pass

    transparent_pixel = b'\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00\x21\xf9\x04\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b'
    return send_file(io.BytesIO(transparent_pixel), mimetype='image/gif')

@app.route('/sw.js')
def sw():
    return app.send_static_file('sw.js')

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)