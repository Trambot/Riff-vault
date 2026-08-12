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

# --- DATABASE CONFIGURATION ---
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + os.path.join(BASE_DIR, "riff_vault.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# --- CLOUDINARY CONFIGURATION ---
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
    print("☁️ Running deep-crawl Cloudinary sync...")
    try:
        # Fetch resources across the entire account without restricting to root only
        response = cloudinary.api.resources(
            resource_type="video", 
            type="upload", 
            max_results=500
        )
        resources = response.get('resources', [])
        print(f"📦 Found {len(resources)} total resources in Cloudinary account.")
        
        for res in resources:
            public_id = res.get('public_id') 
            if not public_id or 'cover' in public_id.lower():
                continue

            # Automatically extract the folder name from the public_id path 
            # (e.g., "New folder/song_name" -> folder_name = "New folder")
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
                
        print("✅ Deep-crawl sync complete!")
    except Exception as e:
        print(f"❌ Cloudinary sync failed: {e}")

# Build DB and Sync before the app starts handling requests (Crucial for Gunicorn/Render)
with app.app_context():
    db.create_all()
    sync_library()

# --- ROUTES ---
@app.route("/")
@app.route("/")
def home():
    # Force a live background sync on every page load
    try:
        sync_library()
    except Exception:
        pass

    # For an SPA, we always load ALL tracks and ALL playlists on the first hit.
    # The JavaScript frontend will handle the hiding/showing without reloading the page!
    all_playlists = db.session.query(Playlist).all()
    all_tracks = db.session.query(Track).all()
    song_requests = db.session.query(SongRequest).all()
        
    return render_template("home.html", 
                           app_name="Riff Vault", 
                           tracks=all_tracks, 
                           playlists=all_playlists, 
                           song_requests=song_requests)

from flask import jsonify, request

@app.route("/create_playlist_ajax", methods=["POST"])
def create_playlist():
    data = request.get_json()
    name = data.get("name")
    if name and not db.session.query(Playlist).filter_by(name=name).first():
        new_pl = Playlist(name=name, is_auto=False)
        db.session.add(new_pl)
        db.session.commit()
        return jsonify({"success": True, "id": new_pl.id, "name": new_pl.name})
    return jsonify({"success": False}), 400

@app.route("/delete_playlist_ajax/<int:pl_id>", methods=["POST"])
def delete_playlist(pl_id):
    pl = db.session.get(Playlist, pl_id)
    if pl and not pl.is_auto: 
        db.session.delete(pl)
        db.session.commit()
        return jsonify({"success": True})
    return jsonify({"success": False}), 400

@app.route("/add_to_playlist_ajax", methods=["POST"])
def add_to_playlist():
    data = request.get_json()
    track_id, playlist_id = data.get("track_id"), data.get("playlist_id")
    if track_id and playlist_id:
        track = db.session.get(Track, int(track_id))
        playlist = db.session.get(Playlist, int(playlist_id))
        if track and playlist and not playlist.is_auto and track not in playlist.tracks:
            playlist.tracks.append(track)
            db.session.commit()
            return jsonify({"success": True})
    return jsonify({"success": False}), 400

@app.route("/remove_from_playlist_ajax", methods=["POST"])
def remove_from_playlist():
    data = request.get_json()
    track_id, playlist_id = data.get("track_id"), data.get("playlist_id")
    if track_id and playlist_id:
        try:
            track = db.session.get(Track, int(track_id))
            playlist = db.session.get(Playlist, int(playlist_id))
            if track and playlist and track in playlist.tracks:
                playlist.tracks.remove(track)
                db.session.commit()
                return jsonify({"success": True})
        except ValueError:
            pass 
    return jsonify({"success": False}), 400

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