#!/usr/bin/env python3
import os
import subprocess

# Config
BASE_DIR = os.getcwd()
AVATAR_PATH = os.path.join(BASE_DIR, "assets/avatars/shiva_hacker.png")

def render_elite_master(video_id, title_main, scenes, output_filename):
    AUDIO_DIR = os.path.join(BASE_DIR, f"label-truth/video-0{video_id}")
    ASSETS_DIR = os.path.join(BASE_DIR, f"assets/visuals/video{video_id}")
    OUTPUT_DIR = os.path.join(BASE_DIR, f"production/video{video_id}/pro-render")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(ASSETS_DIR, exist_ok=True)

    print(f"🎬 Shiva Studio: Mastering Video #{video_id} - {title_main}...")
    
    file_list = []
    prod_placeholder = os.path.join(ASSETS_DIR, "placeholder_prod.png")
    subprocess.run(f"ffmpeg -y -f lavfi -i color=c=white:s=600x800:d=1 -vf \"drawtext=text='PRODUCT':fontcolor=black:fontsize=50:x=(w-text_w)/2:y=(h-text_h)/2\" -update 1 {prod_placeholder}", shell=True)

    for i, scene in enumerate(scenes):
        output_path = os.path.join(OUTPUT_DIR, f"scene_{i}.mp4")
        audio = os.path.join(AUDIO_DIR, scene['audio'])
        
        # Check if audio exists, if not use a silence or skip
        if not os.path.exists(audio):
            # Try full narration name pattern
            alt_audio = os.path.join(AUDIO_DIR, "full-narration.mp3")
            if os.path.exists(alt_audio): audio = alt_audio
            else: continue

        duration = subprocess.check_output(f"ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 {audio}", shell=True).decode().strip()
        
        # Split duration if using a full narration file instead of individual clips
        clip_duration = float(duration) / len(scenes)
        start_time = i * clip_duration

        filter_complex = (
            f"color=c={scene['bg']}:s=1920x1080:d={clip_duration}[bg];"
            f"movie={AVATAR_PATH},scale=300:300[av];"
            f"movie={prod_placeholder},scale=500:-1[prod];"
            f"[bg][av]overlay=x=50:y=730[v1];"
            f"[v1][prod]overlay=x=1240:y=140[v2];"
            f"[v2]drawtext=text='{scene['title']}':fontcolor=white:fontsize=80:x=400:y=150:shadowcolor=black:shadowx=5:shadowy=5[v3];"
            f"[v3]drawtext=text='{scene['dialogue']}':fontcolor=white:fontsize=40:x=400:y=280"
        )

        cmd = f"ffmpeg -y -ss {start_time} -t {clip_duration} -i {audio} -filter_complex \"{filter_complex}\" -c:v libx264 -pix_fmt yuv420p -preset ultrafast -shortest {output_path}"
        subprocess.run(cmd, shell=True)
        file_list.append(f"file '{output_path}'")
    
    list_path = os.path.join(OUTPUT_DIR, "concat.txt")
    with open(list_path, "w") as f:
        f.write("\n".join(file_list))
        
    final_path = os.path.join(OUTPUT_DIR, output_filename)
    subprocess.run(f"ffmpeg -y -f concat -safe 0 -i {list_path} -c copy {final_path}", shell=True)
    print(f"🔥 MASTER DELIVERED: {final_path}")
    return final_path

# Video 1: Cheerios Scenes
v1_scenes = [
    {"audio": "hook.mp3", "title": "CHEERIOS EXPOSÉ", "bg": "0x444400", "dialogue": "The Heart-Shaped Trap Revealed"},
    {"audio": "front-label.mp3", "title": "LABEL DECEPTION", "bg": "black", "dialogue": "1g Fiber vs 3g Health Requirement"},
    {"audio": "ingredients.mp3", "title": "CHEMICAL FORTIFICATION", "bg": "0x330000", "dialogue": "Lab-sourced vitamins sprayed on grain"},
    {"audio": "hidden-truth.mp3", "title": "GLYPHOSATE REALITY", "bg": "0x002200", "dialogue": "Parts-per-billion residue data"},
    {"audio": "verdict.mp3", "title": "FINAL VERDICT", "bg": "gray", "dialogue": "6.8 out of 10 score"}
]

# Video 2: Doritos Scenes
v2_scenes = [
    {"audio": "full-narration.mp3", "title": "DORITOS ILLUSION", "bg": "0x660000", "dialogue": "Addictive Chemistry in a Bag"},
    {"audio": "full-narration.mp3", "title": "BANNED DYES", "bg": "0x884400", "dialogue": "Red 40 and Yellow 5 stats"},
    {"audio": "full-narration.mp3", "title": "SATIETY HIJACK", "bg": "black", "dialogue": "MSG and Disodium Guanylate"},
    {"audio": "full-narration.mp3", "title": "SERVING SIZE TRAP", "bg": "0x222222", "dialogue": "Who stops at only twelve chips?"},
    {"audio": "full-narration.mp3", "title": "CHEMICAL CRUNCH", "bg": "red", "dialogue": "Eat at your own risk"}
]

if __name__ == "__main__":
    v1_master = render_elite_master(1, "Cheerios", v1_scenes, "ELITE_MASTER_CHEERIOS.mp4")
    v2_master = render_elite_master(2, "Doritos", v2_scenes, "ELITE_MASTER_DORITOS.mp4")
