# 24-7
24/7 server

## Modpack server installer

Run `python3 install-modpack.py` and choose CurseForge or Modrinth. The installer
prompts for a project ID and an optional file/version ID; leaving the latter
blank selects the latest server-pack file when available, otherwise the latest
modpack file/version.

Choose **Uninstall a modpack** from the main menu to see the modpack folders
currently installed beside the script. The list is discovered from folders
containing `start.sh`, so removed folders are not listed. Uninstall requires
typing the selected folder name before its files are permanently deleted.

CurseForge requires an API key. If `config.txt` is missing, the installer
creates it beside the script with a placeholder. Replace the placeholder with
your real key in `CF_API_KEY=your_api_key` format and rerun the installer.
Keep the key private; `config.txt` is excluded from Git. The script installs
into a folder named after the modpack, detects the Minecraft version and
supported loader, and downloads the matching Temurin JDK into that folder.
If a server pack is unavailable, it converts the client pack by downloading its
required CurseForge mods or server-compatible Modrinth files, then installs the
pack's declared modloader server runtime. Both loader installation and server
startup use the bundled Java; `start.sh` sets `JAVA_HOME`, `JAVA`, and `PATH` so
the pack uses it too. The installer checks the downloaded JDK version before
use. Each installed modpack has its own folder and `start.sh`. It asks for
confirmation before accepting the Minecraft EULA and running a startup test.
Internet access is required to download the modpack, required mods, loader, and
JDK.
