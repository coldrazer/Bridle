# Aria Answer Sheet

Every question a new user is likely to ask Aria during first launch, engine setup, the assistant wizard, importing models, Quick Assistant, sharing, and the remote-connection wizard — with the answer Aria gives. This document is rendered from `engine/bridle_engine/aria_answers.py`, the same data Aria retrieves from at runtime; edit the Python module and re-render instead of editing this file by hand.

## Starting

### what-is-bridle
- **Asked as:** “what is bridle”; “what does this app do”; “why use bridle”; “how do i get started”; “getting started”; “first steps”
- **Aria answers:** Bridle runs AI assistants privately on your Mac — no account, no cloud. Choose an assistant, download it once, then chat; everything is processed on this Mac. Your next step: tap Set Up Bridle if it is showing.

### internet-needed
- **Asked as:** “do i need internet”; “can i use this offline”; “does it work without wifi”
- **Aria answers:** Internet is needed for setup and new downloads. Once an assistant is downloaded, you can chat fully offline — look for 'Internet is needed for setup and new downloads. Downloaded assistants can chat offline.' on the home screen.

### cost-account
- **Asked as:** “does this cost money”; “do i need an account”; “is there a subscription”; “api costs”
- **Aria answers:** No account and no per-chat cost: the assistants are open models running on your own Mac. You only spend download size and your Mac's power.

### mac-requirements
- **Asked as:** “will this work on my mac”; “what mac do i need”; “intel mac”; “how much memory do i need”; “system requirements”
- **Aria answers:** Bridle needs an Apple silicon Mac (M1 or later) with macOS 14 or newer. More memory means bigger assistants fit; the assistant chooser only recommends ones marked as a comfortable fit for your Mac.

### beginner-vs-advanced
- **Asked as:** “beginner or advanced”; “what is advanced workspace”; “change mode”; “switch to beginner”
- **Aria answers:** Beginner mode guides setup and chat with plain labels. Advanced Workspace (Projects & Tools) adds the full model browser, sharing, performance, and developer tools. Both use the same engine and assistants, and you can switch any time — nothing is lost.

## Privacy

### is-it-private
- **Asked as:** “is this private”; “does my chat go to the cloud”; “is my data sent anywhere”; “who can see my conversations”
- **Aria answers:** Your chats and documents stay on this Mac and are processed locally. Bridle only uses the internet for setup, downloads, and updates. Network sharing is off unless you turn it on in Settings.

### where-data-lives
- **Asked as:** “where are my files stored”; “where do downloads go”; “where is my chat saved”
- **Aria answers:** Bridle keeps its models, settings, and chats in its own folder in Application Support (com.bridle.app). Downloaded assistants live in its models folder; Aria's chat with you is saved separately there too.

### history-privacy
- **Asked as:** “is my prompt history stored”; “request history text”; “log content setting”
- **Aria answers:** By default history keeps only timing, token counts, and errors — 'Prompt text isn't stored (Settings → Performance)'. Turning content logging on stores prompt and answer text on this Mac for debugging; leave it off for normal use. Clear History removes the records.

## Aria

### who-is-aria
- **Asked as:** “who are you”; “what are you”; “are you the assistant”; “what model are you”
- **Aria answers:** I'm Aria, Bridle's built-in guide. I use a small model bundled with the app, so I work even before you download a main assistant. Ask me about setup, choosing an assistant, or fixing problems — for real conversations, chat with your main assistant.

### aria-vs-main
- **Asked as:** “difference between you and my assistant”; “why not just chat with you”; “can you answer my homework”; “general questions”
- **Aria answers:** I'm a tiny guide who only knows Bridle. Your main assistant is much smarter and answers general questions. If you ask me something unrelated, I'll point you to the main chat — that's deliberate, so I don't guess.

### aria-actions-approval
- **Asked as:** “can you download that for me”; “why do i have to approve”; “did you change something”; “can you click buttons”
- **Aria answers:** I can suggest an action — download an assistant, load it, or open a screen — but it only runs when you tap the Approve button on my suggestion card. Approval never carries over: I will always ask again for the next action.

### aria-memory-clear
- **Asked as:** “do you remember me”; “clear our chat”; “forget this conversation”; “delete aria history”
- **Aria answers:** Our conversation is saved on this Mac so I can follow your setup across visits. Tap Clear Chat at the top of my window to delete it; nothing is sent anywhere either way.

### aria-wrong
- **Asked as:** “that answer was wrong”; “you gave bad advice”; “why did you say that”
- **Aria answers:** Sorry about that — I'm a very small model and I can get Bridle details wrong. Cross-check with the Guide button on the home screen, and tell me what you actually see; I'll suggest the next step from there.

### aria-repair
- **Asked as:** “aria is broken”; “aria wont answer”; “repair aria”; “aria failed to start”
- **Aria answers:** Tap Repair Aria in my window (it reloads my bundled model). If repair doesn't work, the Beginner Guide covers the same setup steps, and restarting Bridle from the home screen usually clears engine trouble.

### aria-not-in-build
- **Asked as:** “why is aria missing”; “aria isn't in this build”; “aria unavailable”
- **Aria answers:** This copy of Bridle was built without my bundled model. Everything else still works: use the Guide on the home screen for setup help, or install an official Bridle build that includes me.

### aria-speed-size
- **Asked as:** “how big is aria”; “does aria use much memory”; “aria download size”
- **Aria answers:** I'm bundled with the app (about 140 MB on disk) and keep only a small slice of memory next to your main assistant. I don't download anything and I never replace the assistant you're chatting with.

## Setup

### setup-what
- **Asked as:** “what does set up bridle do”; “what is the engine”; “why setup”; “what installs”
- **Aria answers:** Set Up Bridle installs the engine: a private Python environment plus the MLX software that runs assistants on your Mac's chip. It happens once, needs internet, and can take a few minutes. Tap Set Up Bridle to start it.

### setup-how-long
- **Asked as:** “how long does setup take”; “setup is slow”; “setup stuck”
- **Aria answers:** Setup usually takes a few minutes on a normal connection; the progress shows in Dev Tools. Keep Bridle open and your Mac online. If it looks stuck for over 10 minutes, note the last line shown and use Try Starting Again after restarting the app.

### setup-python-missing
- **Asked as:** “python not found”; “install python”; “python 3.10 error”
- **Aria answers:** Bridle needs Python 3.10 or newer. Install it with 'brew install python@3.12' in Terminal (or from python.org), then tap Set Up Bridle again.

### setup-failed
- **Asked as:** “setup failed”; “engine won't start”; “engine stopped while starting”; “try starting again”
- **Aria answers:** Tap Try Starting Again on the home screen. If it keeps failing, open Dev Tools to see the engine log — a port already in use is retried automatically on a free port, and setup failures name the exact step that failed.

### setup-close-app
- **Asked as:** “can i close the app during setup”; “quit during install”; “close window setup”
- **Aria answers:** Keep Bridle open during setup and downloads; closing it stops the work. Downloads resume where they left off, but setup may need to restart its current step.

## Troubleshooting

### engine-crashed
- **Asked as:** “engine crashed”; “crash report”; “engine keeps restarting”; “crash notice”
- **Aria answers:** Bridle saves a crash report, restarts the engine, and reloads your assistant automatically; it stops retrying after 3 crashes in 5 minutes. Use Open Report on the crash notice if you want the details, or find reports under Crash Reports in Settings.

## Choosing

### choose-which
- **Asked as:** “which assistant should i choose”; “which model is best”; “recommend a model”; “what should i download first”
- **Aria answers:** Start with what you'll do most: Everyday for questions and planning, Writing for drafts, Coding for programming, Photo for pictures. Bridle recommends a comfortable fit for your Mac at the top of Choose Assistant — start there; you can add others later.

### categories
- **Asked as:** “everyday writing coding photo difference”; “what do the categories mean”; “assistant types”; “which assistant for writing”; “choose a writing assistant”; “assistant for coding”
- **Aria answers:** Everyday answers questions and plans. Writing drafts and rewrites. Coding explains and writes code. Photo discusses an image you attach (it needs a one-time Photo support download). Pick the one matching your first task — switching later is free.

### fit-memory
- **Asked as:** “what does fit mean”; “will this model fit”; “tight too large”; “memory warning”; “estimated memory”
- **Aria answers:** Each assistant shows an estimated memory need versus your Mac's memory. 'Fits' is comfortable, 'tight' is risky with other apps open, and too-large models are not recommended. The chooser already hides bad fits — trust Recommended for your Mac.

### too-big-load-anyway
- **Asked as:** “load anyway”; “model too big what happens”; “ignore memory warning”
- **Aria answers:** Load Anyway forces a model that the memory check thinks won't fit. Your Mac may slow down heavily or the load may fail. Safer: close memory-heavy apps first, or choose the Everyday-sized option and load that instead.

### change-later
- **Asked as:** “can i change assistant later”; “switch models”; “have multiple assistants”; “delete an assistant”
- **Aria answers:** Yes. Download as many as fit your disk, switch with Choose Assistant, and remove one from the model list when you need space back. Only one chat assistant is loaded at a time; switching replaces the one in memory, your chats stay.

### draft-model
- **Asked as:** “what is a draft model”; “draft candidate”; “speculative decoding beginner”
- **Aria answers:** A draft is a tiny, fast assistant from the same family that guesses words ahead so the big assistant can answer faster, with identical results. It is optional and mostly an Advanced topic — beginners can ignore it.

## Downloading

### download-how
- **Asked as:** “how do i download an assistant”; “get a model”; “download steps”
- **Aria answers:** Tap Choose Assistant, pick a category, then Download on the recommended assistant. Keep Bridle open and online until it finishes — then it loads and you can chat. I can also suggest the download; you approve it on my card.

### download-size-time
- **Asked as:** “how big is the download”; “how long will download take”; “download size”
- **Aria answers:** Each assistant lists its download size (roughly 300 MB for tiny ones, several GB for large ones) and the progress bar shows speed and percentage. Time depends on your connection; downloads resume if interrupted.

### download-stuck-cancel
- **Asked as:** “download stuck”; “cancel download”; “download slow”; “pause download”
- **Aria answers:** Check your internet first — downloads need it. You can cancel from the download progress and retry later; Bridle keeps the partial files and resumes instead of starting over. A very slow first minute is normal while it verifies what it already has.

### download-failed
- **Asked as:** “download failed”; “download error”; “retry download”; “corrupt download”
- **Aria answers:** Use Retry on the download progress. If it fails repeatedly, check disk space (large assistants need several GB free) and your connection. Files are checksum-verified, so a corrupt piece is re-fetched rather than used.

### disk-space
- **Asked as:** “not enough disk space”; “disk full”; “how much space do i need”
- **Aria answers:** You need the assistant's size plus about 1 GB spare. Free space by deleting an assistant you don't use from the model list, emptying Trash, then retry the download.

### photo-support
- **Asked as:** “photo support install”; “adding photo support”; “photo assistant extra download”; “vision not installed”
- **Aria answers:** Photo help needs a one-time support download (vision software) before the Photo assistant opens. Bridle offers Install when you first pick Photo; keep it online until 'Adding photo support…' finishes, then load the assistant again.

## First chat

### chat-wont-start
- **Asked as:** “why won't chat start”; “chat is disabled”; “can't chat”; “no assistant loaded”
- **Aria answers:** Chat needs three things: setup finished, an assistant downloaded, and that assistant loaded. Check the home screen steps in order — if a download is still running, wait for it; if nothing is downloaded, use Choose Assistant first.

### loading-slow
- **Asked as:** “loading takes forever”; “getting your assistant ready stuck”; “first answer slow”
- **Aria answers:** The first load reads several GB from disk and the first answer warms the model up, so both can take a little while — later answers are faster. If 'Getting your assistant ready…' lasts over a few minutes, close other heavy apps and use Retry loading selected model.

### starters
- **Asked as:** “what should i ask first”; “conversation starters”; “prompt ideas”; “try a starter”
- **Aria answers:** Tap one of the Try a conversation starter ideas, or just describe what you need ('help me plan…', 'explain… in simple terms'). Adding a little context — who it's for, how long — gets noticeably better answers.

### stop-generation
- **Asked as:** “stop the answer”; “cancel reply”; “answer too long”
- **Aria answers:** Use the Stop control while an answer is generating. You can then ask a shorter follow-up like 'make that shorter' — your conversation so far is kept.

### attach-photo
- **Asked as:** “how do i attach a photo”; “send an image”; “ask about a picture”
- **Aria answers:** Load the Photo assistant first, then use the plus (Attach Image) button beside your message and pick the image. Dragging an image onto the chat also works. The Everyday assistant cannot see pictures.

### model-sleeping
- **Asked as:** “model sleeping”; “assistant unloaded itself”; “idle unload”; “why reload every time”; “stop using memory when idle”; “free memory when not in use”
- **Aria answers:** To free memory, Bridle unloads an idle assistant after a while and reloads it automatically on your next message — that's the 'Sleeping' state, not an error. You can change or disable the idle time in Settings.

### context-full
- **Asked as:** “context too long”; “conversation too long”; “chat forgot earlier messages”
- **Aria answers:** Very long chats keep the most recent part that fits the assistant's memory; the oldest turns are left out of new requests (your saved transcript is unchanged). Starting a New Conversation gives a clean slate when that matters.

## Your models

### use-my-models
- **Asked as:** “use my models”; “import models i already have”; “models on my disk”; “hugging face cache”
- **Aria answers:** Tap Use My Models, Choose Folder… on the folder holding your models (or its parent), then Scan Folder. Supported MLX/Safetensors folders appear as candidates; Add & Host Selected Model registers one without copying or moving your files.

### import-unsupported
- **Asked as:** “gguf not supported”; “no supported model folder found”; “my model won't import”; “missing tokenizer”
- **Aria answers:** Bridle hosts MLX/Safetensors models only — GGUF files can't run here, and a folder needs config.json, tokenizer files, and .safetensors weights. The wizard lists exactly what was rejected and why; download the MLX version of that model instead.

### import-external-disk
- **Asked as:** “models on external drive”; “keep disk plugged in”; “imported model disappeared”
- **Aria answers:** Imported models stay where they are — Bridle only records the location. Keep that disk or folder available; if it goes offline, the assistant shows as unavailable until it returns. Removing an imported entry only removes the reference, never your files.

## Quick Assistant

### quick-assistant
- **Asked as:** “quick assistant shortcut”; “control option space”; “ask bridle from another app”; “selected text service”
- **Aria answers:** Press Control–Option–Space anywhere to open Quick Assistant, or use the Assistant menu. You can also select text in another app and use Services → Ask Bridle. Your main assistant must be loaded for it to answer.

### voice-talk
- **Asked as:** “how does talk work”; “voice input”; “microphone”; “talk button needs speech model”
- **Aria answers:** In Quick Assistant, Talk records a short message and transcribes it on this Mac with a downloaded speech assistant (Models → Speech). If Talk says to get one first, download it there, then try again — nothing is recorded until you tap Talk.

### mic-permission
- **Asked as:** “microphone permission denied”; “mic not working”; “allow microphone”
- **Aria answers:** Open System Settings → Privacy & Security → Microphone and allow Bridle, then try Talk again. Bridle only records while you are in a Talk session, and cancelled recordings are removed.

### screenshot-permission
- **Asked as:** “screenshot not working”; “screen recording permission”; “ask about my screen”
- **Aria answers:** Screenshot capture needs Screen Recording permission (System Settings → Privacy & Security → Screen Recording → Bridle; reopen Bridle after granting). The shot is attached to chat — ask about it with the Photo assistant loaded, since other assistants cannot see images.

### menu-bar
- **Asked as:** “menu bar icon”; “keep serving window closed”; “close window still run”; “launch at login”
- **Aria answers:** The menu bar icon shows status and lets you switch assistants or copy the endpoint. In Settings you can keep Bridle serving with the window closed and launch it at login; Quit Bridle from the menu stops everything.

### spoken-replies
- **Asked as:** “read replies aloud”; “text to speech”; “spoken answers”
- **Aria answers:** Turn on Read replies in Quick Assistant and completed replies are read with a macOS voice. Stop interrupts the speaking. It uses system voices only — no extra download.

## Sharing

### share-phone
- **Asked as:** “share with my phone”; “use bridle from another device”; “serve on network”; “connect an app”
- **Aria answers:** In Settings, turn on network sharing and Serve HTTPS, then create a named API key for that device (Create Key — copy it at once, only its hash is stored). Other apps point at the copied endpoint with that key. Keep sharing off until you need it.

### certificate-warning
- **Asked as:** “certificate not trusted”; “https fingerprint”; “self signed warning”; “verify certificate”
- **Aria answers:** Bridle's HTTPS uses its own self-signed certificate, so clients warn until they trust it. Compare the SHA-256 fingerprint shown in the Inspector with what the client reports — continue only if they match exactly.

### api-key-lost
- **Asked as:** “lost my api key”; “show key again”; “revoke key”; “key compromised”
- **Aria answers:** Keys cannot be shown again (only their hash is kept). Create a new key for that device, then Revoke the lost one in Settings — revoking takes effect immediately and doesn't affect other devices.

### someone-else-access
- **Asked as:** “can others on my wifi use this”; “is sharing safe”; “who can connect”
- **Aria answers:** With sharing off, only apps on this Mac can connect. With sharing on, remote callers need HTTPS and a valid named key; anything else is rejected. Give each device its own key so you can revoke just that device.

## Projects & Tools

### studio-what
- **Asked as:** “what is projects and tools”; “what is studio”; “documents jobs workflows”
- **Aria answers:** Projects & Tools (Studio) is the workspace for documents with cited answers, background jobs, workflows, evaluations, and advanced controls like performance and remote connections. Beginners don't need it; it's there when you want more than chat.

### documents-ask
- **Asked as:** “ask my documents”; “chat with a pdf”; “cited answers”; “scanned pdf ocr”
- **Aria answers:** In Studio → Documents you import a text file or text-based PDF, then ask questions and get answers with numbered source citations. Finding passages is keyword-based, and scanned PDFs need readable text (OCR) first.

### jobs-background
- **Asked as:** “background jobs”; “run a prompt later”; “job interrupted restart”
- **Aria answers:** Jobs run a prompt in the background and keep results across restarts; interrupted jobs need an explicit Retry so nothing reruns silently. You can cancel a running job from the Jobs list.

### performance-heat
- **Asked as:** “mac getting hot”; “fan noise”; “battery drain”; “quiet profile”; “slow down generation”; “temperature speed”
- **Aria answers:** Big assistants work the chip hard. Smaller ones are far cooler and faster. In Studio → Power, the Quiet profile reduces background work; macOS Low Power or high heat also slows generation system-wide. Nothing here changes your fans directly.

### speed-expectations
- **Asked as:** “how fast should it be”; “tokens per second”; “why is it slow”
- **Aria answers:** Speed depends on the model size, your chip, and how long the conversation is. Small assistants answer quickly; multi-GB ones are slower, especially on the first answer. Performance in Studio shows real measured speeds for your Mac.

## Remote workspaces

### remote-what
- **Asked as:** “what is a remote connection”; “another computer workspace”; “does the model run there”
- **Aria answers:** A remote connection lets a task use a folder on another Mac or Linux computer over SSH. The folder is the workspace; your assistant still runs on this Mac — connecting never starts a model there or shares memory.

### remote-alias-vs-host
- **Asked as:** “host alias or hostname”; “ssh alias meaning”; “username and port”
- **Aria answers:** Use an SSH host alias if you already have one in ~/.ssh/config (it carries host, user, and port). Otherwise choose Hostname and type the machine's name or address, your username there, and port 22 unless that machine uses a different SSH port.

### remote-trust
- **Asked as:** “fingerprint trust”; “verify host key”; “is this host safe”; “changed key blocked”
- **Aria answers:** On first contact Bridle shows the server's SHA256 fingerprint and waits for you to Trust This Host — compare it with the server's owner or console before trusting. Bridle stores that trust only for itself; if the key ever changes, the connection is blocked rather than silently accepted.

### remote-key-agent
- **Asked as:** “ssh key locked”; “agent not available”; “ssh-add”; “key file reference”; “password login”
- **Aria answers:** Bridle never asks for or stores passwords, passphrases, or key contents. For encrypted keys, load yours into the agent first (run ssh-add in Terminal), or pick Key file and point at the existing file — it's referenced, never copied. Password sign-in isn't offered.

### remote-folder-access
- **Asked as:** “read only or write remote folder”; “use this workspace”; “remote symlink”
- **Aria answers:** Read-only is the default and enough for browsing and reading. Request write access only for tasks that must save there; writes still need a preview you approve, bound to the exact file and content. Symlinked folders are labelled, and paths that escape the chosen root are refused.

### remote-task-binding
- **Asked as:** “bind workspace to task”; “runs on model runs on”; “change task workspace”; “offline workspace badge”
- **Aria answers:** Pick the workspace in the task's Workspace control before starting — the task card then shows 'Runs on: <device> as <user> · Model runs on: This Mac'. The choice is frozen into the task; to change it, start a new task. Saved workspaces stay visible, marked offline, while that computer is away.

### remote-forget
- **Asked as:** “forget connection”; “remove remote computer”; “delete workspace”
- **Aria answers:** Forget removes Bridle's references, trust, and saved workspaces for that computer and stops its connections; it does not delete any remote files or change that machine's SSH settings. Existing tasks keep the location they were bound with.

## Updates

### updates
- **Asked as:** “update bridle”; “check for updates”; “automatic updates”
- **Aria answers:** Use Check for Updates… in the Bridle menu (official builds check automatically if enabled in Settings). Updating the app also updates Aria's bundled model when it changes. Your assistants, chats, and settings are kept.

### uninstall-reset
- **Asked as:** “uninstall bridle”; “delete everything”; “reset bridle”; “remove all data”
- **Aria answers:** Quit Bridle, move the app to Trash, and delete the com.bridle.app folder in Application Support to remove models, chats, keys, and settings. That folder is several GB if you downloaded large assistants, so check its size first.

