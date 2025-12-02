import discord
from discord.ext import commands
import sqlite3
import time
import io
import csv
import os  # <-- This is the new import to handle environment variables

# --- Configuration ---
# Fetch token securely from Replit Secrets (Environment Variables)
# NOTE: You MUST set the DISCORD_TOKEN secret in the Replit Environment tab.
TOKEN = os.getenv('DISCORD_TOKEN') 
DB_NAME = 'coworkbot.db'
# Primary Teal: 0x0FA3B1, Slate Gray: 0x2D3A45
EMBED_COLOR = 0x0FA3B1 
# Timeout for waiting for a description after !logout (seconds)
LOGOUT_TIMEOUT = 60 

# --- Database Setup ---
def init_db():
    """Initializes the SQLite database and creates the sessions table if it doesn't exist."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            username TEXT,
            start_ts INTEGER NOT NULL,   -- unix epoch seconds
            end_ts INTEGER,              -- null for active sessions
            duration_minutes INTEGER,    -- computed at logout
            description TEXT,
            created_at INTEGER DEFAULT (strftime('%s','now'))
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_user ON sessions(user_id)")
    conn.commit()
    conn.close()

# Setup Intents (Permissions for the bot to see events)
# Intents must match the settings enabled in the Discord Developer Portal
intents = discord.Intents.default()
intents.message_content = True  # Required to read commands like !login
intents.members = True          # Required for accurate username logging and reports

# Set up the bot client
bot = commands.Bot(command_prefix='!', intents=intents)

# --- Utility Functions ---
def get_current_session(user_id):
    """Retrieves the active (un-ended) session for a given user."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM sessions WHERE user_id = ? AND end_ts IS NULL", (user_id,))
    session = cursor.fetchone()
    conn.close()
    return session

def get_session_duration(session):
    """Calculates the duration in minutes for a session."""
    start_ts = session[3] # start_ts column
    end_ts = int(time.time())
    duration_seconds = end_ts - start_ts
    return round(duration_seconds / 60)

# --- Events ---
@bot.event
async def on_ready():
    """Called when the bot has finished logging in and setting things up."""
    init_db()
    print(f'CoWorkBot is online as {bot.user}')
    print(f'Connected to database: {DB_NAME}')
    # Set the bot's status
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="productivity (!help)"))

# --- User Commands ---

@bot.command(name='login')
async def login(ctx):
    """Starts a new work session."""
    user_id = str(ctx.author.id)
    username = str(ctx.author)

    if get_current_session(user_id):
        session = get_current_session(user_id)
        start_time = time.strftime('%H:%M:%S', time.gmtime(session[3]))
        
        embed = discord.Embed(
            title="⚠️ Already Logged In",
            description=f"You are already clocked in since **{start_time}** (UTC). Please use `!logout` to end your current session.",
            color=EMBED_COLOR
        )
        await ctx.send(embed=embed)
        return

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    current_ts = int(time.time())
    
    # Insert new session with end_ts and duration_minutes as NULL
    cursor.execute("INSERT INTO sessions (user_id, username, start_ts) VALUES (?, ?, ?)", 
                   (user_id, username, current_ts))
    conn.commit()
    conn.close()

    embed = discord.Embed(
        title="✅ Clocked In!",
        description=f"Happy working! Use `!logout` when you finish.",
        color=EMBED_COLOR
    )
    await ctx.send(embed=embed)


@bot.command(name='logout')
async def logout(ctx):
    """Ends the current work session and prompts for a description."""
    user_id = str(ctx.author.id)
    session = get_current_session(user_id)

    if not session:
        embed = discord.Embed(
            title="❌ Not Logged In",
            description="You are not currently clocked in. Use `!login` to start a session.",
            color=0xFF0000  # Red for error
        )
        await ctx.send(embed=embed)
        return

    # Prompt the user for a description
    prompt = await ctx.send("🛑 Clocking out... 📝 **What did you work on?** (Reply in this channel within 60 seconds)")

    def check(m):
        # Checks that the message is from the original user and in the same channel
        return m.author == ctx.author and m.channel == ctx.channel

    try:
        # Wait for the user's reply
        description_msg = await bot.wait_for('message', check=check, timeout=LOGOUT_TIMEOUT)
        description = description_msg.content.strip()

    except asyncio.TimeoutError:
        description = "No description provided (Timed out)."
        await ctx.send(f"Timeout reached. Saving session with description: **{description}**")

    # Finalize the session
    session_id = session[0]
    duration_minutes = get_session_duration(session)
    end_ts = int(time.time())
    
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE sessions 
        SET end_ts = ?, duration_minutes = ?, description = ? 
        WHERE id = ?
    """, (end_ts, duration_minutes, description, session_id))
    conn.commit()
    conn.close()

    hours = int(duration_minutes // 60)
    minutes = int(duration_minutes % 60)
    
    embed = discord.Embed(
        title="✅ Session Logged!",
        description=f"**Duration:** {hours}h {minutes}m\n**Task:** {description}",
        color=0x00FF00  # Green for success
    )
    await ctx.send(embed=embed)


@bot.command(name='status')
async def status(ctx):
    """Shows the duration of the current work session."""
    user_id = str(ctx.author.id)
    session = get_current_session(user_id)

    if not session:
        embed = discord.Embed(
            title="🔄 Status Check",
            description="You are currently **clocked out**. Use `!login` to start a session.",
            color=EMBED_COLOR
        )
        await ctx.send(embed=embed)
        return

    duration_minutes = get_session_duration(session)
    hours = int(duration_minutes // 60)
    minutes = int(duration_minutes % 60)
    
    start_time = time.strftime('%H:%M:%S', time.gmtime(session[3]))

    embed = discord.Embed(
        title="🔄 Status Check",
        description=f"You have been working for **{hours}h {minutes}m**.\nClocked in since: {start_time} (UTC)",
        color=EMBED_COLOR
    )
    await ctx.send(embed=embed)


@bot.command(name='leaderboard')
async def leaderboard(ctx):
    """Shows the top 10 users by total work time."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # Get total minutes for all users with completed sessions
    cursor.execute("""
        SELECT username, SUM(duration_minutes) as total_minutes 
        FROM sessions 
        WHERE duration_minutes IS NOT NULL
        GROUP BY user_id, username
        ORDER BY total_minutes DESC
        LIMIT 10
    """)
    results = cursor.fetchall()
    conn.close()

    if not results:
        return await ctx.send("No completed sessions logged yet!")

    leaderboard_text = []
    for i, (username, total_minutes) in enumerate(results):
        hours = int(total_minutes // 60)
        minutes = int(total_minutes % 60)
        leaderboard_text.append(f"**#{i+1}:** {username} - {hours}h {minutes}m")
    
    embed = discord.Embed(
        title="🏆 CoWorkBot Leaderboard (Top 10)",
        description="\n".join(leaderboard_text),
        color=EMBED_COLOR
    )
    await ctx.send(embed=embed)

# --- Admin Commands ---

@bot.command(name='report')
@commands.has_permissions(administrator=True)
async def report(ctx, target_user: discord.Member = None):
    """
    (Admin Only) Shows total work stats for a user or the entire server.
    Usage: !report [@user]
    """
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    if target_user:
        # Report for a specific user
        user_id = str(target_user.id)
        username = str(target_user)
        
        cursor.execute("""
            SELECT SUM(duration_minutes), COUNT(id) 
            FROM sessions 
            WHERE user_id = ? AND duration_minutes IS NOT NULL
        """, (user_id,))
        total_minutes, session_count = cursor.fetchone()
        
        if total_minutes is None:
            embed = discord.Embed(
                title=f"📊 Report for {username}",
                description="No completed work sessions found.",
                color=EMBED_COLOR
            )
        else:
            total_hours = int(total_minutes // 60)
            total_minutes_rem = int(total_minutes % 60)
            
            embed = discord.Embed(
                title=f"📊 Work Report for {username}",
                description=f"**Total Sessions:** {session_count}\n**Total Time Logged:** {total_hours}h {total_minutes_rem}m",
                color=EMBED_COLOR
            )
        await ctx.send(embed=embed)

    else:
        # Overall server report (Top 5 for simplicity)
        cursor.execute("""
            SELECT username, SUM(duration_minutes) as total_minutes, COUNT(id) as session_count
            FROM sessions 
            WHERE duration_minutes IS NOT NULL
            GROUP BY user_id, username
            ORDER BY total_minutes DESC
            LIMIT 5
        """)
        results = cursor.fetchall()

        if not results:
            return await ctx.send("No completed sessions logged in the database yet.")

        report_summary = []
        for username, total_minutes, session_count in results:
            total_hours = int(total_minutes // 60)
            total_minutes_rem = int(total_minutes % 60)
            report_summary.append(f"**{username}**: {total_hours}h {total_minutes_rem}m across {session_count} sessions")

        embed = discord.Embed(
            title="🌐 Server Work Summary (Top 5)",
            description="\n".join(report_summary),
            color=EMBED_COLOR
        )
        await ctx.send(embed=embed)
        
    conn.close()

@report.error
async def report_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ **Error:** You need Administrator permissions to run the `!report` command.")
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send("❌ **Error:** Could not find that member. Please mention them (e.g., `!report @user`).")
    else:
        print(f"Report Command Error: {error}")
        await ctx.send("❌ An unexpected error occurred while running the report.")


@bot.command(name='export')
@commands.has_permissions(administrator=True)
async def export_data(ctx):
    """
    (Admin Only) Exports all session data as a CSV file.
    """
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    # Select all completed sessions
    cursor.execute("SELECT * FROM sessions WHERE duration_minutes IS NOT NULL ORDER BY start_ts DESC")
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return await ctx.send("The database is empty. Nothing to export.")

    # Prepare data for CSV
    s_buf = io.StringIO()
    writer = csv.writer(s_buf)

    # Write header
    header = ['id', 'user_id', 'username', 'start_ts', 'end_ts', 'duration_minutes', 'description', 'created_at']
    writer.writerow(header)
    
    # Write data rows
    for row in rows:
        writer.writerow(row)
    
    s_buf.seek(0)
    
    # Send as file
    await ctx.send(
        "📊 **Data Export**", 
        file=discord.File(fp=io.BytesIO(s_buf.getvalue().encode()), filename="coworkbot_data.csv")
    )


# Need this import for the asyncio.TimeoutError in !logout
import asyncio 

# Run the bot (this will use the TOKEN fetched from os.getenv('DISCORD_TOKEN'))
if TOKEN is None:
    print("FATAL ERROR: DISCORD_TOKEN environment variable not found. Please set it in Replit Secrets!")
else:
    bot.run(TOKEN)
