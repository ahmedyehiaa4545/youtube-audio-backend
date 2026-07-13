# Dedicated YouTube Audio Downloader Backend (Railway)

This is a standalone, lightweight backend service designed to run on **Railway** to download audio from YouTube videos using `yt-dlp` and `ffmpeg` with a custom low-bitrate setting (64k MP3), completely bypassing the SSL handshake limits and blockages of Hugging Face Spaces.

## How to Deploy on Railway

1. **Create a new GitHub Repository**:
   - Create a clean new private or public repository on GitHub (e.g., `youtube-audio-downloader`).
2. **Push this folder**:
   - Initialize a git repository inside the `youtube-audio-backend` folder:
     ```bash
     cd youtube-audio-backend
     git init
     git add .
     git commit -m "Initial commit"
     git branch -M main
     git remote add origin <your-new-github-repo-url>
     git push -u origin main
     ```
3. **Deploy to Railway**:
   - Go to [Railway.app](https://railway.app/) and log in.
   - Click **New Project** -> **Deploy from GitHub repo**.
   - Choose your new `youtube-audio-downloader` repository.
   - Railway will automatically detect the `Dockerfile`, build it, and run the service.
4. **Generate a Domain**:
   - Once deployed, go to the service settings in Railway, find **Networking**, and click **Generate Domain** (or set up a custom domain). This will give you a URL like `https://xxx.up.railway.app`.

## How to connect to your Frontend

Update the `audioApiUrl` variable in your frontend files:
- In `netlify-deploy/app.js` (line 58):
  ```javascript
  let audioApiUrl = 'https://your-new-railway-domain.up.railway.app';
  ```
- In `frontend/src/App.tsx` (or where the audio download tab is called), set the URL accordingly.
