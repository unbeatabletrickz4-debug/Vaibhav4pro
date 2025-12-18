import os
import logging
import asyncio
import subprocess
import signal
import sys
import psutil
import json
import threading
import shutil
from flask import Flask, request, render_template_string, jsonify
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, 
    MessageHandler, filters, ConversationHandler, CallbackQueryHandler
)

# --- CONFIGURATION ---
TOKEN = os.environ.get("TOKEN") 
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0")) 
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:8080")

UPLOAD_DIR = "scripts"
if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)

USERS_FILE = "allowed_users.json"
OWNERSHIP_FILE = "ownership.json"

running_processes = {} 
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- FLASK SERVER & UNIVERSAL EDITOR ---
app = Flask(__name__)

EDITOR_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Universal Editor</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <!-- CodeMirror Core -->
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.2/codemirror.min.css">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.2/theme/dracula.min.css">
    <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.2/codemirror.min.js"></script>
    
    <!-- Modes -->
    <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.2/mode/python/python.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.2/mode/javascript/javascript.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.2/mode/shell/shell.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.2/mode/properties/properties.min.js"></script>

    <style>
        body { margin: 0; padding: 0; background: #282a36; color: #f8f8f2; font-family: sans-serif; display: flex; flex-direction: column; height: 100vh; }
        .header { padding: 10px; background: #44475a; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #6272a4; }
        .header h3 { margin: 0; font-size: 14px; }
        .btn { background: #50fa7b; color: #282a36; border: none; padding: 8px 15px; border-radius: 5px; font-weight: bold; cursor: pointer; }
        .CodeMirror { flex-grow: 1; font-size: 13px; }
    </style>
</head>
<body>
    <div class="header">
        <h3>✏️ {{ filename }}</h3>
        <button class="btn" onclick="saveCode()">💾 Save & Restart</button>
    </div>
    <textarea id="code_area">{{ code }}</textarea>
    <script>
        var tg = window.Telegram.WebApp;
        tg.expand(); 
        
        // Auto-detect mode based on extension passed from python
        var ext = "{{ filename }}".split('.').pop();
        var mode = "python";
        if(ext === "js" || ext === "json") mode = "javascript";
        if(ext === "sh") mode = "shell";
        if(ext === "txt" || ext === "env") mode = "properties";

        var editor = CodeMirror.fromTextArea(document.getElementById("code_area"), {
            mode: mode, theme: "dracula", lineNumbers: true
        });

        function saveCode() {
            fetch('/save_code', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ 
                    target_id: "{{ target_id }}", 
                    code: editor.getValue(),
                    file_type: "{{ file_type }}"
                })
            })
            .then(r => r.json())
            .then(data => {
                if(data.status === 'success') {
                    tg.showAlert("✅ Saved & Restarting...");
                    tg.close();
                } else {
                    tg.showAlert("❌ Error: " + data.message);
                }
            });
        }
    </script>
</body>
</html>
"""

@app.route('/')
def home(): return "🤖 Polyglot Host Bot is Alive!", 200

@app.route('/status')
def script_status():
    script_name = request.args.get('script')
    if not script_name: return "Specify script", 400
    if script_name in running_processes and running_processes[script_name]['process'].poll() is None:
        return f"✅ {script_name} is running.", 200
    return f"❌ {script_name} is stopped.", 404

@app.route('/editor')
def editor_page():
    target_id = request.args.get('id')
    uid = int(request.args.get('uid', 0))
    file_type = request.args.get('type', 'src') 
    
    owner = get_owner(target_id)
    if uid != ADMIN_ID and uid != owner: return "⛔ Access Denied"
    
    work_dir, _, _, req_path, full_script_path = resolve_paths(target_id)
    
    file_path = full_script_path
    if file_type == 'env':
        if "|" in target_id: file_path = os.path.join(work_dir, ".env")
        else: file_path = os.path.join(work_dir, f"{target_id}.env")
    elif file_type == 'req':
        # Smart detect: requirements.txt OR package.json
        if os.path.exists(os.path.join(work_dir, "package.json")):
             file_path = os.path.join(work_dir, "package.json")
        else:
             file_path = req_path
        
    content = ""
    if os.path.exists(file_path):
        with open(file_path, 'r') as f: content = f.read()
    
    return render_template_string(EDITOR_HTML, code=content, target_id=target_id, filename=os.path.basename(file_path), file_type=file_type)

@app.route('/save_code', methods=['POST'])
def save_code_route():
    data = request.json
    target_id = data.get('target_id')
    code = data.get('code')
    file_type = data.get('file_type')
    
    work_dir, _, _, req_path, full_script_path = resolve_paths(target_id)
    
    save_path = full_script_path
    if file_type == 'env':
        if "|" in target_id: save_path = os.path.join(work_dir, ".env")
        else: save_path = os.path.join(work_dir, f"{target_id}.env")
    elif file_type == 'req':
        # Check if it looks like package.json
        if "{" in code and "dependencies" in code:
            save_path = os.path.join(work_dir, "package.json")
        else:
            save_path = req_path

    try:
        with open(save_path, 'w') as f: f.write(code)
        
        # Auto-Install Dependencies
        if file_type == 'req':
            if save_path.endswith("package.json"):
                 subprocess.check_call(["npm", "install"], cwd=work_dir)
            else:
                 subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", save_path])
        
        restart_process_background(target_id)
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

# --- LOGIC: POLYGLOT RUNNER ---
def resolve_run_command(script_path):
    """Determines the command based on file extension."""
    ext = script_path.split('.')[-1].lower()
    if ext == 'py':
        return ["python", "-u", script_path]
    elif ext == 'js':
        return ["node", script_path]
    elif ext == 'sh':
        return ["bash", script_path]
    else:
        # Default fallback (Try Python)
        return ["python", "-u", script_path]

def restart_process_background(target_id):
    work_dir, script_path, env_path, _, _ = resolve_paths(target_id)
    
    if target_id in running_processes:
        try: os.killpg(os.getpgid(running_processes[target_id]['process'].pid), signal.SIGTERM)
        except: pass
    
    custom_env = os.environ.copy()
    if os.path.exists(env_path):
        with open(env_path) as f:
            for l in f:
                if '=' in l and not l.strip().startswith('#'):
                    k,v = l.strip().split('=', 1)
                    custom_env[k.strip()] = v.strip().strip('"').strip("'")
    
    log_path = os.path.join(UPLOAD_DIR, f"{target_id.replace('|','_')}.log")
    log_file = open(log_path, "w")
    
    # Get Dynamic Command (Python/Node/Bash)
    cmd = resolve_run_command(script_path)
    
    try:
        proc = subprocess.Popen(cmd, env=custom_env, stdout=log_file, stderr=subprocess.STDOUT, cwd=work_dir, preexec_fn=os.setsid)
        running_processes[target_id] = {"process": proc, "log": log_path}
    except Exception as e:
        logger.error(f"Failed to start {target_id}: {e}")

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

# --- STANDARD FUNCTIONS ---
def get_allowed_users():
    if not os.path.exists(USERS_FILE): return []
    try:
        with open(USERS_FILE, 'r') as f: return json.load(f)
    except: return []

def save_allowed_user(uid):
    users = get_allowed_users()
    if uid not in users:
        users.append(uid)
        with open(USERS_FILE, 'w') as f: json.dump(users, f)
        return True
    return False

def remove_allowed_user(uid):
    users = get_allowed_users()
    if uid in users:
        users.remove(uid)
        with open(USERS_FILE, 'w') as f: json.dump(users, f)
        return True
    return False

def load_ownership():
    if not os.path.exists(OWNERSHIP_FILE): return {}
    try:
        with open(OWNERSHIP_FILE, 'r') as f: return json.load(f)
    except: return {}

def save_ownership(target_id, user_id, type_):
    data = load_ownership()
    data[target_id] = {"owner": user_id, "type": type_}
    with open(OWNERSHIP_FILE, 'w') as f: json.dump(data, f)

def delete_ownership(target_id):
    data = load_ownership()
    if target_id in data:
        del data[target_id]
        with open(OWNERSHIP_FILE, 'w') as f: json.dump(data, f)

def get_owner(target_id):
    data = load_ownership()
    return data.get(target_id, {}).get("owner")

def restricted(func):
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        uid = update.effective_user.id
        if uid != ADMIN_ID and uid not in get_allowed_users():
            await update.message.reply_text("⛔ Access Denied.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

def super_admin_only(func):
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if update.effective_user.id != ADMIN_ID:
            await update.message.reply_text("⛔ Super Admin Only.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

def resolve_paths(target_id):
    if "|" in target_id:
        repo, file = target_id.split("|")
        work_dir = os.path.join(UPLOAD_DIR, repo)
        script_path = file
        env_path = os.path.join(work_dir, ".env")
        req_path = os.path.join(work_dir, "requirements.txt")
        full_script_path = os.path.join(work_dir, script_path)
    else:
        work_dir = UPLOAD_DIR
        script_path = target_id
        env_path = os.path.join(work_dir, f"{target_id}.env")
        req_path = os.path.join(work_dir, f"{target_id}_req.txt")
        full_script_path = os.path.join(work_dir, target_id)
    return work_dir, script_path, env_path, req_path, full_script_path

# --- POLYGLOT INSTALLER ---
async def install_dependencies(work_dir, update):
    # Check for requirements.txt (Python)
    req_txt = os.path.join(work_dir, "requirements.txt")
    # Check for package.json (Node)
    pkg_json = os.path.join(work_dir, "package.json")
    
    msg = None
    if os.path.exists(req_txt) or os.path.exists(pkg_json):
        msg = await update.message.reply_text("⏳ **Installing Dependencies...**")
    
    try:
        # Python
        if os.path.exists(req_txt):
            proc = await asyncio.create_subprocess_exec("pip", "install", "-r", req_txt, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            await proc.communicate()
            
        # Node.js
        if os.path.exists(pkg_json):
            proc = await asyncio.create_subprocess_exec("npm", "install", cwd=work_dir, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            await proc.communicate()

        if msg: await msg.edit_text("✅ **Dependencies Installed!**")
    except Exception as e:
        if msg: await msg.edit_text(f"❌ Install Error: {e}")

# --- KEYBOARDS ---
def main_menu_keyboard():
    return ReplyKeyboardMarkup([
        ["📤 Upload File", "🌐 Clone from Git"],
        ["📂 My Hosted Apps", "📊 Server Stats"],
        ["🆘 Help"]
    ], resize_keyboard=True)

def extras_keyboard():
    return ReplyKeyboardMarkup([
        ["➕ Add Deps", "📝 Type Env Vars"], 
        ["🚀 RUN NOW", "🔙 Cancel"]
    ], resize_keyboard=True)

def git_extras_keyboard():
    return ReplyKeyboardMarkup([
        ["📝 Type Env Vars"],
        ["📂 Select File to Run", "🔙 Cancel"]
    ], resize_keyboard=True)

# --- BOT HANDLERS ---
WAIT_FILE, WAIT_EXTRAS, WAIT_ENV_TEXT = range(3)

@restricted
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 **Polyglot Hosting Bot**\nSupports: Python, Node.js, Bash", reply_markup=main_menu_keyboard())

@restricted
async def upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📤 Send file (`.py`, `.js`, `.sh`)", reply_markup=ReplyKeyboardMarkup([['🔙 Cancel']], resize_keyboard=True))
    return WAIT_FILE

async def receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "🔙 Cancel": return await cancel(update, context)
    file = await update.message.document.get_file()
    fname = update.message.document.file_name
    uid = update.effective_user.id
    
    # Validation
    allowed_exts = ['.py', '.js', '.sh']
    if not any(fname.endswith(ext) for ext in allowed_exts):
        return await update.message.reply_text(f"❌ Only {allowed_exts} allowed.")

    owner = get_owner(fname)
    if os.path.exists(os.path.join(UPLOAD_DIR, fname)) and owner and owner != uid and uid != ADMIN_ID: return await update.message.reply_text("❌ Taken.")
    
    path = os.path.join(UPLOAD_DIR, fname)
    await file.download_to_drive(path)
    save_ownership(fname, uid, "file")
    context.user_data.update({'type': 'file', 'target_id': fname, 'work_dir': UPLOAD_DIR})
    await update.message.reply_text(f"✅ Saved.", reply_markup=extras_keyboard())
    return WAIT_EXTRAS

async def receive_extras(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text
    if txt == "🚀 RUN NOW": return await execute_logic(update, context)
    elif txt == "🔙 Cancel": return await cancel(update, context)
    elif txt == "📝 Type Env Vars":
        await update.message.reply_text("📝 **Type Env Vars:**", reply_markup=ReplyKeyboardMarkup([['🔙 Cancel']], resize_keyboard=True))
        return WAIT_ENV_TEXT
    elif "Deps" in txt:
        await update.message.reply_text("📂 Send `requirements.txt` or `package.json`.")
        context.user_data['wait'] = 'req'
        return WAIT_EXTRAS
    return WAIT_EXTRAS

async def receive_env_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "🔙 Cancel": return await cancel(update, context)
    target_id = context.user_data['target_id']
    _, _, env_path, _, _ = resolve_paths(target_id)
    with open(env_path, "a") as f:
        if os.path.exists(env_path) and os.path.getsize(env_path) > 0: f.write("\n")
        f.write(update.message.text)
    
    if context.user_data.get('type') == 'repo': 
        await update.message.reply_text("✅ Saved.", reply_markup=git_extras_keyboard())
        return WAIT_GIT_EXTRAS
    await update.message.reply_text("✅ Saved.", reply_markup=extras_keyboard())
    return WAIT_EXTRAS

async def receive_extra_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('wait'): return WAIT_EXTRAS
    file = await update.message.document.get_file()
    fname = update.message.document.file_name
    target_id = context.user_data['target_id']
    
    # Universal Dep Handler
    path = ""
    if fname == "package.json":
        # For single file mode, we treat UPLOAD_DIR as root
        path = os.path.join(UPLOAD_DIR, "package.json")
    elif fname.endswith(".txt"):
        path = os.path.join(UPLOAD_DIR, f"{target_id}_req.txt")
    
    if path:
        await file.download_to_drive(path)
        await install_dependencies(UPLOAD_DIR, update)
        
    context.user_data['wait'] = None
    await update.message.reply_text("Next?", reply_markup=extras_keyboard())
    return WAIT_EXTRAS

# --- GIT HANDLERS ---
WAIT_URL, WAIT_GIT_EXTRAS, WAIT_GIT_ENV_TEXT, WAIT_SELECT_FILE = range(3, 7)

@restricted
async def git_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🌐 **Git URL**", reply_markup=ReplyKeyboardMarkup([['🔙 Cancel']], resize_keyboard=True))
    return WAIT_URL

async def receive_git_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    if url == "🔙 Cancel": return await cancel(update, context)
    repo_name = url.split("/")[-1].replace(".git", "")
    repo_path = os.path.join(UPLOAD_DIR, repo_name)
    if os.path.exists(repo_path): shutil.rmtree(repo_path)
    try:
        subprocess.check_call(["git", "clone", url, repo_path])
        await install_dependencies(repo_path, update)
        context.user_data.update({'repo_path': repo_path, 'repo_name': repo_name, 'target_id': f"{repo_name}|PLACEHOLDER", 'type': 'repo', 'work_dir': repo_path})
        await update.message.reply_text("⚙️ **Setup**", reply_markup=git_extras_keyboard())
        return WAIT_GIT_EXTRAS
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
        return ConversationHandler.END

async def receive_git_extras(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text
    if txt == "🔙 Cancel": return await cancel(update, context)
    elif txt == "📝 Type Env Vars":
        await update.message.reply_text("📝 **Type Env Vars:**", reply_markup=ReplyKeyboardMarkup([['🔙 Cancel']], resize_keyboard=True))
        return WAIT_GIT_ENV_TEXT
    elif txt == "📂 Select File to Run": return await show_file_selection(update, context)
    return WAIT_GIT_EXTRAS

async def show_file_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    repo_path = context.user_data['repo_path']
    files_found = []
    for root, dirs, files in os.walk(repo_path):
        for file in files:
            # Polyglot Filter
            if file.endswith((".py", ".js", ".sh")): 
                files_found.append(os.path.relpath(os.path.join(root, file), repo_path))
    
    if not files_found: return await update.message.reply_text("❌ No executable files found.")
    
    # Pagination or limit to 10
    keyboard = [[InlineKeyboardButton(f, callback_data=f"sel_py_{f}")] for f in files_found[:15]]
    await update.message.reply_text("👇 **Select Main File:**", reply_markup=InlineKeyboardMarkup(keyboard))
    return WAIT_SELECT_FILE

async def select_git_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    filename = query.data.split("sel_py_")[1]
    repo_name = context.user_data['repo_name']
    unique_id = f"{repo_name}|{filename}"
    save_ownership(unique_id, update.effective_user.id, "repo")
    context.user_data['target_id'] = unique_id
    await query.edit_message_text(f"✅ Selected `{filename}`")
    return await execute_logic(query, context)

# --- EXECUTION ---
async def execute_logic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg_func = update.message.reply_text if update.message else update.callback_query.message.reply_text
    target_id = context.user_data.get('target_id', context.user_data.get('fallback_id'))
    work_dir, script_path, env_path, _, _ = resolve_paths(target_id)

    if target_id in running_processes:
        try:
            os.killpg(os.getpgid(running_processes[target_id]['process'].pid), signal.SIGTERM)
            running_processes[target_id]['process'].wait()
        except: pass
        del running_processes[target_id]

    custom_env = os.environ.copy()
    if os.path.exists(env_path):
        with open(env_path) as f:
            for l in f:
                if '=' in l and not l.strip().startswith('#'):
                    k,v = l.strip().split('=', 1)
                    custom_env[k.strip()] = v.strip().strip('"').strip("'")

    log_file_path = os.path.join(UPLOAD_DIR, f"{target_id.replace('|','_')}.log")
    log_file = open(log_file_path, "w")
    
    # Universal Command Resolver
    cmd = resolve_run_command(script_path)
    
    try:
        proc = subprocess.Popen(cmd, env=custom_env, stdout=log_file, stderr=subprocess.STDOUT, cwd=work_dir, preexec_fn=os.setsid)
        running_processes[target_id] = {"process": proc, "log": log_file_path}
        
        await msg_func(f"🚀 **Started!**\nID: `{target_id}`\nPID: {proc.pid}")
        await asyncio.sleep(3)
        if proc.poll() is not None:
            log_file.close()
            with open(log_file_path) as f: log = f.read()[-2000:]
            await msg_func(f"❌ **Crashed:**\n`{log}`", parse_mode="Markdown", reply_markup=main_menu_keyboard())
        else:
            url = f"{BASE_URL}/status?script={target_id}"
            await msg_func(f"🟢 **Running!**\n🔗 URL: `{url}`", parse_mode="Markdown", reply_markup=main_menu_keyboard())
    except Exception as e: await msg_func(f"❌ Error: {e}", reply_markup=main_menu_keyboard())
    return ConversationHandler.END

# --- MANAGE CALLBACKS ---
@restricted
async def list_hosted(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ownership = load_ownership()
    keyboard = []
    for tid, meta in ownership.items():
        if uid == ADMIN_ID or uid == meta.get("owner"):
            status = "🟢" if tid in running_processes and running_processes[tid]['process'].poll() is None else "🔴"
            keyboard.append([InlineKeyboardButton(f"{status} {tid}", callback_data=f"man_{tid}")])
    if not keyboard: return await update.message.reply_text("📂 No files.", reply_markup=main_menu_keyboard())
    await update.message.reply_text("📂 **Your Apps:**", reply_markup=InlineKeyboardMarkup(keyboard))

async def manage_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    uid = update.effective_user.id

    if data.startswith("man_"):
        tid = data.split("man_")[1]
        owner = get_owner(tid)
        if uid != ADMIN_ID and uid != owner: return await query.message.reply_text("⛔ Not yours.")
        is_running = tid in running_processes and running_processes[tid]['process'].poll() is None
        
        text = f"⚙️ **Manage:** `{tid}`\nStatus: {'🟢 Running' if is_running else '🔴 Stopped'}"
        
        btns = []
        if is_running:
            btns.append([InlineKeyboardButton("🛑 Stop", callback_data=f"stop_{tid}"), InlineKeyboardButton("🔗 URL", callback_data=f"url_{tid}")])
        else:
            btns.append([InlineKeyboardButton("🚀 Run", callback_data=f"rerun_{tid}")])
        
        # Determine editor type for Dependencies
        dep_type = "req" # default
        # If it's a JS file, we assume package.json
        if tid.endswith(".js"): dep_type = "req" 

        btns.append([
            InlineKeyboardButton("✏️ Source", web_app=WebAppInfo(url=f"{BASE_URL}/editor?id={tid}&type=src&uid={uid}")),
            InlineKeyboardButton("✏️ Env", web_app=WebAppInfo(url=f"{BASE_URL}/editor?id={tid}&type=env&uid={uid}"))
        ])
        btns.append([
            InlineKeyboardButton("✏️ Deps", web_app=WebAppInfo(url=f"{BASE_URL}/editor?id={tid}&type=req&uid={uid}")),
            InlineKeyboardButton("📜 Logs", callback_data=f"log_{tid}")
        ])
        btns.append([InlineKeyboardButton("🗑️ Delete", callback_data=f"del_{tid}")])
        
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(btns), parse_mode="Markdown")

    elif data.startswith("stop_"):
        tid = data.split("stop_")[1]
        if tid in running_processes:
            os.killpg(os.getpgid(running_processes[tid]['process'].pid), signal.SIGTERM)
            await query.edit_message_text(f"🛑 Stopped `{tid}`")
            
    elif data.startswith("rerun_"):
        context.user_data['fallback_id'] = data.split("rerun_")[1]
        await query.delete_message()
        await execute_logic(update, context)

    elif data.startswith("del_"):
        tid = data.split("del_")[1]
        if tid in running_processes:
            try: os.killpg(os.getpgid(running_processes[tid]['process'].pid), signal.SIGTERM)
            except: pass
            del running_processes[tid]
        delete_ownership(tid)
        work_dir, _, _, _, _ = resolve_paths(tid)
        if "|" in tid: shutil.rmtree(work_dir, ignore_errors=True)
        else: 
             try: os.remove(os.path.join(UPLOAD_DIR, tid))
             except: pass
        await query.edit_message_text(f"🗑️ Deleted `{tid}`")

    elif data.startswith("log_"):
        tid = data.split("log_")[1]
        path = os.path.join(UPLOAD_DIR, f"{tid.replace('|','_')}.log")
        if os.path.exists(path): await context.bot.send_document(chat_id=update.effective_chat.id, document=open(path, 'rb'))
        else: await query.message.reply_text("❌ No logs.")

    elif data.startswith("url_"):
        tid = data.split("url_")[1]
        await query.message.reply_text(f"🔗 `{BASE_URL}/status?script={tid}`", parse_mode="Markdown")

# --- ADMIN & SYSTEM ---
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 Cancelled.", reply_markup=main_menu_keyboard())
    return ConversationHandler.END

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🆘 **Help**\nContact: @platoonleaderr", parse_mode="Markdown")

@super_admin_only
async def add_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if save_allowed_user(int(context.args[0])): await update.message.reply_text("✅ Added.")

@super_admin_only
async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if remove_allowed_user(int(context.args[0])): await update.message.reply_text("🗑️ Removed.")

@restricted
async def server_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"📊 Running: {len(running_processes)}")

if __name__ == '__main__':
    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()
    
    app_bot = ApplicationBuilder().token(TOKEN).build()

    conv_file = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📤 Upload File$"), upload_start)],
        states={
            WAIT_FILE: [MessageHandler(filters.Regex("^🔙 Cancel$"), cancel), MessageHandler(filters.Document.ALL, receive_file)],
            WAIT_EXTRAS: [MessageHandler(filters.Regex("^🔙 Cancel$"), cancel), MessageHandler(filters.Regex("^(🚀|➕|📝)"), receive_extras), MessageHandler(filters.Document.ALL, receive_extra_files)],
            WAIT_ENV_TEXT: [MessageHandler(filters.Regex("^🔙 Cancel$"), cancel), MessageHandler(filters.TEXT, receive_env_text)]
        },
        fallbacks=[CommandHandler('cancel', cancel)], per_message=False
    )

    conv_git = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^🌐 Clone from Git$"), git_start)],
        states={
            WAIT_URL: [MessageHandler(filters.Regex("^🔙 Cancel$"), cancel), MessageHandler(filters.TEXT, receive_git_url)],
            WAIT_GIT_EXTRAS: [MessageHandler(filters.Regex("^🔙 Cancel$"), cancel), MessageHandler(filters.Regex("^(📝|📂)"), receive_git_extras)],
            WAIT_GIT_ENV_TEXT: [MessageHandler(filters.Regex("^🔙 Cancel$"), cancel), MessageHandler(filters.TEXT, receive_env_text)],
            WAIT_SELECT_FILE: [CallbackQueryHandler(select_git_file)]
        },
        fallbacks=[CommandHandler('cancel', cancel)], per_message=False 
    )
    
    app_bot.add_handler(CommandHandler('add', add_user))
    app_bot.add_handler(CommandHandler('remove', remove_user))
    app_bot.add_handler(conv_file)
    app_bot.add_handler(conv_git)
    app_bot.add_handler(MessageHandler(filters.Regex("^📂 My Hosted Apps$"), list_hosted))
    app_bot.add_handler(MessageHandler(filters.Regex("^📊 Server Stats$"), server_stats))
    app_bot.add_handler(MessageHandler(filters.Regex("^🆘 Help$"), help_command))
    app_bot.add_handler(CallbackQueryHandler(manage_callback))
    app_bot.add_handler(CommandHandler('start', start))

    print("Bot is up and running!")
    app_bot.run_polling()
