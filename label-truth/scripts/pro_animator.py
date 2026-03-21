#!/usr/bin/env python3
import os
import subprocess

# Config
BASE_DIR = os.getcwd()
AUDIO_DIR = os.path.join(BASE_DIR, "label-truth/video-03")
OUTPUT_DIR = os.path.join(BASE_DIR, "production/video3/pro-render")
os.makedirs(OUTPUT_DIR, exist_ok=True)

SCENES = [
    {
        "audio": "hook.mp3",
        "title": "PLANT MILK REALITY",
        "subtitle": "Bypassing the Fillers",
        "bg_color": "0x1a1a1a", # Dark Professional Grey
        "accent": "0x00FF00"      # Green
    },
    {
        "audio": "good-brand.mp3",
        "title": "THE ELMHURST STANDARD",
        "subtitle": "Only 5 Real Ingredients",
        "bg_color": "0x0a2e0a", # Forest Green
        "accent": "0xFFD700"      # Gold
    },
    {
        "audio": "hydrorelease.mp3",
        "title": "HYDRO-RELEASE™ TECH",
        "subtitle": "Preserving 20g Whole Grain",
        "bg_color": "0x003366", # Tech Blue
        "accent": "0x00CCFF" 
    },
    {
        "audio": "proof.mp3",
        "title": "VERIFIED CLEAN",
        "subtitle": "Glyphosate Residue Free",
        "bg_color": "0xffffff", # Contrast White
        "accent": "0x333333"
    },
    {
        "audio": "verdict.mp3",
        "title": "CHOOSE QUALITY",
        "subtitle": "Support Honest Business",
        "bg_color": "0xdaa520", # Goldenrod
        "accent": "0xffffff"
    }
]

def render_pro_scene(index, scene):
    audio_path = os.path.join(AUDIO_DIR, scene['audio'])
    output_path = os.path.join(OUTPUT_DIR, f"pro_scene_{index}.mp4")
    
    # Get audio duration
    duration = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", audio_path]).decode().strip()
    
    # Complex Filter Design:
    # 1. Base color background
    # 2. Animated 'Particle' effect (using noise or vignetting)
    # 3. Dynamic Title (Kinetic Typography)
    # 4. Progress bar at the bottom
    
    title = scene['title'].replace("'", "")
    subtitle = scene['subtitle'].replace("'", "")
    bg = scene['bg_color']
    accent = scene['accent']

    # Filter Graph: 
    # Create background -> add slight vignette movement -> Draw text with shadow and alpha fade-in
    filter_complex = (
        f"color=c={bg}:s=1920x1080:d={duration}[bg];"
        f"[bg]vignette=angle='PI/4+sin(t)*0.1'[v1];"
        f"[v1]drawtext=text='{title}':fontcolor=white:fontsize=90:x=(w-text_w)/2:y=(h-text_h)/2-50:"
        f"alpha='if(lt(t,0.5),t/0.5,1)':shadowcolor=black:shadowx=5:shadowy=5[v2];"
        f"[v2]drawtext=text='{subtitle}':fontcolor={accent}:fontsize=50:x=(w-text_w)/2:y=(h-text_h)/2+60:"
        f"alpha='if(lt(t,1),0,if(lt(t,1.5),(t-1)/0.5,1))'[v3];"
        f"[v3]drawbox=y=ih-10:w=iw*t/{duration}:h=10:color={accent}:t=fill[outv]"
    )

    cmd = [
        "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", # Placeholder audio needed for filter mapping sometimes
        "-i", audio_path,
        "-filter_complex", filter_complex,
        "-map", "[outv]", "-map", "1:a",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-shortest", output_path
    ]
    
    subprocess.run(cmd)

def assemble_pro():
    print("💎 Shiva Pro-Editor: Executing kinetic typography render...")
    file_list = []
    for i, scene in enumerate(SCENES):
        clip_path = os.path.join(OUTPUT_DIR, f"pro_scene_{i}.mp4")
        render_pro_scene(i, scene)
        file_list.append(f"file '{clip_path}'")
        
    list_path = os.path.join(OUTPUT_DIR, "concat.txt")
    with open(list_path, "w") as f:
        f.write("\n".join(file_list))
        
    final_path = os.path.join(OUTPUT_DIR, "PRO_LABEL_TRUTH_V3_ELMHURST.mp4")
    concat_cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", final_path]
    subprocess.run(concat_cmd)
    print(f"🔥 Elite Asset Delivered: {final_path}")

if __name__ == "__main__":
    assemble_pro()
