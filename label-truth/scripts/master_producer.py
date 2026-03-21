#!/usr/bin/env python3
import os
import subprocess

# Config
BASE_DIR = os.getcwd()
AUDIO_DIR = os.path.join(BASE_DIR, "label-truth/video-03")
ASSETS_DIR = os.path.join(BASE_DIR, "assets/visuals/video3")
AVATAR_PATH = os.path.join(BASE_DIR, "assets/avatars/shiva_hacker.png")
OUTPUT_DIR = os.path.join(BASE_DIR, "production/video3/final_master")
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(ASSETS_DIR, exist_ok=True)

# Scene Mapping - Using simple descriptions for dialogue to avoid escape issues
SCENES = [
    {"audio": "hook.mp3", "title": "PLANT MILK REALITY", "bg": "black", "dialogue": "Filtered water and real ingredients only"},
    {"audio": "good-brand.mp3", "title": "THE ELMHURST STANDARD", "bg": "darkgreen", "dialogue": "No gums. No fillers. No seed oils."},
    {"audio": "hydrorelease.mp3", "title": "HYDRO-RELEASE TECH", "bg": "blue", "dialogue": "Science keeping the nutrient profile intact"},
    {"audio": "proof.mp3", "title": "VERIFIED CLEAN", "bg": "gray", "dialogue": "Certified Glyphosate Residue Free"},
    {"audio": "verdict.mp3", "title": "ELITE QUALITY", "bg": "gold", "dialogue": "Choose a brand that respects the ingredient"}
]

def setup_assets():
    # 1. Avatar
    if not os.path.exists(AVATAR_PATH):
        os.makedirs(os.path.dirname(AVATAR_PATH), exist_ok=True)
        subprocess.run(f"ffmpeg -y -f lavfi -i color=c=orange:s=400x400:d=1 -vf \"drawtext=text='SHIVA':fontcolor=black:fontsize=100:x=(w-text_w)/2:y=(h-text_h)/2\" -update 1 {AVATAR_PATH}", shell=True)
    
    # 2. Product Placeholder (White card)
    prod_placeholder = os.path.join(ASSETS_DIR, "placeholder_prod.png")
    subprocess.run(f"ffmpeg -y -f lavfi -i color=c=white:s=600x800:d=1 -vf \"drawtext=text='PRODUCT':fontcolor=black:fontsize=50:x=(w-text_w)/2:y=(h-text_h)/2\" -update 1 {prod_placeholder}", shell=True)

def render_scene(i, scene):
    output_path = os.path.join(OUTPUT_DIR, f"scene_{i}.mp4")
    audio = os.path.join(AUDIO_DIR, scene['audio'])
    duration = subprocess.check_output(f"ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 {audio}", shell=True).decode().strip()
    prod_placeholder = os.path.join(ASSETS_DIR, "placeholder_prod.png")

    # Filter Strategy:
    # 1. Background
    # 2. Avatar in bottom left
    # 3. Product in right center
    # 4. Text headers
    
    filter_complex = (
        f"color=c={scene['bg']}:s=1920x1080:d={duration}[bg];"
        f"movie={AVATAR_PATH},scale=300:300[av];"
        f"movie={prod_placeholder},scale=500:-1[prod];"
        f"[bg][av]overlay=x=50:y=730[v1];"
        f"[v1][prod]overlay=x=1200:y=140[v2];"
        f"[v2]drawtext=text='{scene['title']}':fontcolor=white:fontsize=80:x=400:y=150:shadowcolor=black:shadowx=5:shadowy=5[v3];"
        f"[v3]drawtext=text='{scene['dialogue']}':fontcolor=white:fontsize=45:x=400:y=280"
    )

    cmd = f"ffmpeg -y -i {audio} -filter_complex \"{filter_complex}\" -c:v libx264 -pix_fmt yuv420p -preset ultrafast -shortest {output_path}"
    subprocess.run(cmd, shell=True)

def assemble():
    setup_assets()
    print("🎬 Shiva Master: Processing pro-composite...")
    file_list = []
    for i, scene in enumerate(SCENES):
        clip = os.path.join(OUTPUT_DIR, f"scene_{i}.mp4")
        render_scene(i, scene)
        file_list.append(f"file '{clip}'")
    
    with open(os.path.join(OUTPUT_DIR, "concat.txt"), "w") as f:
        f.write("\n".join(file_list))
        
    final = os.path.join(OUTPUT_DIR, "MASTER_ELMHURST_PRO.mp4")
    subprocess.run(f"ffmpeg -y -f concat -safe 0 -i {os.path.join(OUTPUT_DIR, 'concat.txt')} -c copy {final}", shell=True)
    print(f"🔥 MASTER DELIVERED: {final}")

if __name__ == "__main__":
    assemble()
