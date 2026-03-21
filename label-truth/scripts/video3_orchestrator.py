#!/usr/bin/env python3
import os
import subprocess

# Config
BASE_DIR = os.getcwd()
AUDIO_DIR = os.path.join(BASE_DIR, "label-truth/video-03")
ASSETS_DIR = os.path.join(BASE_DIR, "assets/visuals/video3")
OUTPUT_DIR = os.path.join(BASE_DIR, "production/video3/final")
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(ASSETS_DIR, exist_ok=True)

SCENES = [
    {"text": "ELMHURST 1925  THE GOLDEN STANDARD", "audio": "hook.mp3", "color": "darkgreen", "anim": "zoom"},
    {"text": "ONLY 5 INGREDIENTS  NO GUMS NO FILLERS", "audio": "good-brand.mp3", "color": "darkblue", "anim": "slide"},
    {"text": "HYDRORELEASE METHOD  RETAINING NUTRITION", "audio": "hydrorelease.mp3", "color": "orange", "anim": "zoom"},
    {"text": "PRODUCT IMAGE  CERTIFIED GLYPHOSATE FREE", "audio": "proof.mp3", "color": "green", "anim": "slide"},
    {"text": "SUPPORT BETTER BUSINESS", "audio": "verdict.mp3", "color": "gold", "anim": "zoom"}
]

def generate_animated_scene(scene, output_path):
    text = scene['text'].replace("'", "")
    color = scene['color']
    anim = scene['anim']
    audio_path = os.path.join(AUDIO_DIR, scene['audio'])
    
    # Get audio duration
    duration_cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", audio_path]
    duration = subprocess.check_output(duration_cmd).decode().strip()
    
    # Base canvas
    vf_chain = f"drawtext=text='{text}':fontcolor=white:fontsize=70:x=(w-text_w)/2:y=(h-text_h)/2"
    
    if anim == "zoom":
        vf_chain += f",zoompan=z='min(zoom+0.001,1.5)':d=25*{duration}:s=1920x1080"
    elif anim == "slide":
        vf_chain = f"drawtext=text='{text}':fontcolor=white:fontsize=70:x='(w-text_w)/2':y='(h-text_h)/2 + (50*sin(t*2))'" # Floating effect

    cmd = [
        "ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=1920x1080:d={duration}",
        "-i", audio_path,
        "-vf", vf_chain,
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", output_path
    ]
    subprocess.run(cmd)

def assemble_v3():
    print("🚀 Shiva Auto-Creator: Animating Video #3 (The Golden Standard)...")
    file_list = []
    for i, scene in enumerate(SCENES):
        clip_path = os.path.join(OUTPUT_DIR, f"scene_{i}.mp4")
        generate_animated_scene(scene, clip_path)
        file_list.append(f"file '{clip_path}'")
        
    list_path = os.path.join(OUTPUT_DIR, "concat.txt")
    with open(list_path, "w") as f:
        f.write("\n".join(file_list))
        
    final_path = os.path.join(OUTPUT_DIR, "LABEL_TRUTH_V3_ELMHURST.mp4")
    concat_cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", final_path]
    subprocess.run(concat_cmd)
    print(f"✅ Success! Video #3 delivered to: {final_path}")

if __name__ == "__main__":
    # Note: Audio segments will be generated in separate steps if needed
    # For now assume they are ready for the test
    assemble_v3()
