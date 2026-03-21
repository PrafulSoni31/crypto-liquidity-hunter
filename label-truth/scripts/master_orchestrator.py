#!/usr/bin/env python3
import os
import subprocess

# Config
BASE_DIR = os.getcwd()
AUDIO_DIR = os.path.join(BASE_DIR, "label-truth/video-01")
ASSETS_DIR = os.path.join(BASE_DIR, "assets/visuals")
OUTPUT_DIR = os.path.join(BASE_DIR, "production/video1/final")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Scene Mapping: (Audio File, Visual Asset, Duration/Start)
# For the POC, we'll build a command that concatenates clips
SCENES = [
    {"audio": "hook.mp3", "image": "hook_bg.png", "text": "THE HEART SHAPED TRAP"},
    {"audio": "front-label.mp3", "image": "label_reveal.png", "text": "CAN HELP LOWER CHOLESTEROL?"},
    {"audio": "ingredients.mp3", "image": "ingredient_list.png", "text": "WHAT'S REALLY INSIDE?"},
    {"audio": "hidden-truth.mp3", "image": "sugar_comparison.png", "text": "12g ADDED SUGAR"},
    {"audio": "verdict.mp3", "image": "final_verdict.png", "text": "TRUTH REVEALED"}
]

def generate_placeholder_image(text, output_path, color="black", animation_type=None):
    # animation_type could be "zoom", "slide", or "fade" (simplified for FFmpeg logic)
    simple_text = text.replace(":", " ").replace("+", " ").replace(",", " ").replace("?", " ")
    
    # Base command for 1080p canvas with text
    cmd = [
        "ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=1920x1080:d=5",
        "-vf", f"drawtext=text='{simple_text}':fontcolor=white:fontsize=60:x=(w-text_w)/2:y=(h-text_h)/2"
    ]
    
    if animation_type == "zoom":
        # Slow zoom in effect
        cmd[-1] += ",zoompan=z='min(zoom+0.0015,1.5)':d=125:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
    elif animation_type == "slide":
        # Text sliding from left
        cmd[-1] = f"drawtext=text='{simple_text}':fontcolor=white:fontsize=60:x='-w+((t/5)*w*2)':y=(h-text_h)/2"

    cmd += ["-frames:v", "125", "-update", "1", output_path]
    print(f"Generating animated clip: {' '.join(cmd)}")
    subprocess.run(cmd)

def assemble_video_v3():
    # Similar logic to video2 but with animation and specific Elmhurst assets
    pass

def assemble_video():
    print("🚀 Shiva Auto-Creator: Starting forensic assembly...")
    
    file_list = []
    for i, scene in enumerate(SCENES):
        audio_path = os.path.join(AUDIO_DIR, scene['audio'])
        image_path = os.path.join(ASSETS_DIR, scene['image'])
        clip_path = os.path.join(OUTPUT_DIR, f"scene_{i}.mp4")
        
        # Create placeholder image if doesn't exist
        if not os.path.exists(image_path):
            generate_placeholder_image(scene['text'], image_path)
            
        # Get audio duration
        duration_cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", audio_path]
        duration = subprocess.check_output(duration_cmd).decode().strip()
        
        # Create clip
        render_cmd = [
            "ffmpeg", "-y", "-loop", "1", "-i", image_path, "-i", audio_path,
            "-c:v", "libx264", "-t", duration, "-pix_fmt", "yuv420p", "-vf", "scale=1920:1080",
            "-c:a", "aac", "-shortest", clip_path
        ]
        subprocess.run(render_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        file_list.append(f"file '{clip_path}'")
    
    # Concatenate
    list_path = os.path.join(OUTPUT_DIR, "concat.txt")
    with open(list_path, "w") as f:
        f.write("\n".join(file_list))
        
    final_path = os.path.join(OUTPUT_DIR, "LABEL_TRUTH_V1_CHEERIOS.mp4")
    concat_cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", final_path]
    subprocess.run(concat_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    print(f"✅ Success! Asset delivered to: {final_path}")

if __name__ == "__main__":
    assemble_video()
