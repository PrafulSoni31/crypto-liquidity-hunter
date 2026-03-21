#!/usr/bin/env python3
import os
import subprocess

# Config
BASE_DIR = os.getcwd()
AUDIO_DIR = os.path.join(BASE_DIR, "label-truth/video-02")
ASSETS_DIR = os.path.join(BASE_DIR, "assets/visuals/video2")
OUTPUT_DIR = os.path.join(BASE_DIR, "production/video2/final")
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(ASSETS_DIR, exist_ok=True)

# Scene Mapping for Doritos Video
SCENES = [
    {"text": "DORITOS: THE NACHO CHEESE ILLUSION", "audio": "full-narration.mp3", "image": "hook_bg.png", "color": "red"},
    {"text": "SYNTHETIC FLAVOR TRIAD: MSG + DISODIUM GUANYLATE", "audio": "full-narration.mp3", "image": "ingredients.png", "color": "darkred"},
    {"text": "BANNED DYES: RED 40, YELLOW 5, YELLOW 6", "audio": "full-narration.mp3", "image": "dyes.png", "color": "orange"},
    {"text": "THE SERVING SIZE TRAP: 12 CHIPS ONLY?", "audio": "full-narration.mp3", "image": "trap.png", "color": "black"},
    {"text": "VERDICT: CHEMICAL ADDICTION", "audio": "full-narration.mp3", "image": "verdict.png", "color": "red"}
]

def generate_placeholder_image(text, output_path, color="black"):
    # Simplified text for FFmpeg drawtext to avoid parsing errors
    simple_text = text.replace(":", " ").replace("+", " ").replace(",", " ").replace("?", " ")
    cmd = [
        "ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=1920x1080:d=1",
        "-vf", f"drawtext=text='{simple_text}':fontcolor=white:fontsize=60:x=(w-text_w)/2:y=(h-text_h)/2",
        "-frames:v", "1", "-update", "1", output_path
    ]
    print(f"Generating image: {' '.join(cmd)}")
    subprocess.run(cmd)

def assemble_video():
    print("🚀 Shiva Auto-Creator: Starting forensic assembly for Video #2 (Doritos)...")
    
    audio_path = os.path.join(AUDIO_DIR, "full-narration.mp3")
    duration_cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", audio_path]
    total_duration = float(subprocess.check_output(duration_cmd).decode().strip())
    
    scene_duration = total_duration / len(SCENES)
    file_list = []
    
    for i, scene in enumerate(SCENES):
        image_path = os.path.join(ASSETS_DIR, scene['image'])
        clip_path = os.path.join(OUTPUT_DIR, f"scene_{i}.mp4")
        
        if not os.path.exists(image_path):
            generate_placeholder_image(scene['text'], image_path, scene['color'])
            
        # Create clip for this segment of the audio
        start_time = i * scene_duration
        render_cmd = [
            "ffmpeg", "-y", "-loop", "1", "-i", image_path, "-ss", str(start_time), "-t", str(scene_duration), "-i", audio_path,
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-vf", "scale=1920:1080",
            "-c:a", "aac", "-shortest", "-update", "1", clip_path
        ]
        print(f"Running: {' '.join(render_cmd)}")
        subprocess.run(render_cmd)
        file_list.append(f"file '{clip_path}'")
    
    # Concatenate
    list_path = os.path.join(OUTPUT_DIR, "concat.txt")
    with open(list_path, "w") as f:
        f.write("\n".join(file_list))
        
    final_path = os.path.join(OUTPUT_DIR, "LABEL_TRUTH_V2_DORITOS.mp4")
    concat_cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", "-update", "1", final_path]
    print(f"Running: {' '.join(concat_cmd)}")
    subprocess.run(concat_cmd)
    
    print(f"✅ Success! Video #2 delivered to: {final_path}")

if __name__ == "__main__":
    assemble_video()
