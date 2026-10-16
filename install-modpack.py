#!/usr/bin/env python3
"""Install a Modrinth or CurseForge server pack into a named directory."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import tarfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath


API_USER_AGENT = "24-7-modpack-installer/1.0 (server setup)"
SERVER_DIR = Path.cwd()
CONFIG_FILE = Path(__file__).resolve().with_name("config.txt")


class InstallerError(Exception):
    pass


def request(url: str, headers: dict[str, str] | None = None) -> bytes:
    request_headers = {"User-Agent": API_USER_AGENT}
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(url, headers=request_headers)
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            return response.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise InstallerError(f"Request failed for {url}: {exc}") from exc


def get_json(url: str, headers: dict[str, str] | None = None):
    try:
        return json.loads(request(url, headers))
    except json.JSONDecodeError as exc:
        raise InstallerError(f"Server returned invalid JSON for {url}") from exc


def download(url: str, destination: Path, headers: dict[str, str] | None = None):
    destination.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {destination.name}...")
    request_headers = {"User-Agent": API_USER_AGENT}
    if headers:
        request_headers.update(headers)
    req = urllib.request.Request(url, headers=request_headers)
    partial = destination.with_name(destination.name + ".part")
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            with partial.open("wb") as output:
                shutil.copyfileobj(response, output)
        partial.replace(destination)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        partial.unlink(missing_ok=True)
        raise InstallerError(f"Download failed for {url}: {exc}") from exc


def safe_extract(archive: Path, target: Path):
    target = target.resolve()
    try:
        with zipfile.ZipFile(archive) as zf:
            for entry in zf.infolist():
                name = entry.filename.replace("\\", "/")
                relative = PurePosixPath(name)
                if relative.is_absolute() or ".." in relative.parts:
                    raise InstallerError(f"Unsafe path in downloaded archive: {name}")
                output = (target / Path(*relative.parts)).resolve()
                if output != target and target not in output.parents:
                    raise InstallerError(f"Unsafe path in downloaded archive: {name}")
            zf.extractall(target)
    except zipfile.BadZipFile as exc:
        raise InstallerError(f"{archive.name} is not a valid ZIP archive") from exc


def curseforge_file(project_id: str, file_id: str | None, api_key: str):
    headers = {"x-api-key": api_key}
    if file_id:
        url = f"https://api.curseforge.com/v1/mods/{project_id}/files/{file_id}"
        result = get_json(url, headers).get("data")
        if not result:
            raise InstallerError("CurseForge returned no details for that file ID.")
        if not result.get("isServerPack") and result.get("serverPackFileId"):
            server_id = result["serverPackFileId"]
            server_pack = get_json(
                f"https://api.curseforge.com/v1/mods/{project_id}/files/{server_id}",
                headers,
            ).get("data")
            if server_pack and server_pack.get("isServerPack"):
                result = server_pack
        return result, headers

    files = []
    index = 0
    while index < 500:
        response = get_json(
            "https://api.curseforge.com/v1/mods/"
            f"{project_id}/files?pageSize=50&index={index}",
            headers,
        )
        page = response.get("data") or []
        files.extend(page)
        if len(page) < 50:
            break
        index += len(page)
    server_files = [item for item in files if item.get("isServerPack")]
    candidates = server_files or [
        item for item in files if item.get("fileName", "").lower().endswith(".zip")
    ]
    if not candidates:
        raise InstallerError("No downloadable modpack files were found for this project.")
    candidates.sort(key=lambda item: item.get("fileDate", ""), reverse=True)
    return candidates[0], headers


def curseforge_project(project_id: str, api_key: str) -> dict:
    response = get_json(
        f"https://api.curseforge.com/v1/mods/{project_id}",
        {"x-api-key": api_key},
    )
    project = response.get("data")
    if not project or not project.get("name"):
        raise InstallerError("CurseForge did not return a valid modpack name.")
    return project


def curseforge_download_url(project_id: str, file_id: int, file_data: dict, headers: dict):
    if file_data.get("downloadUrl"):
        return file_data["downloadUrl"]
    response = get_json(
        f"https://api.curseforge.com/v1/mods/{project_id}/files/{file_id}/download-url",
        headers,
    )
    url = response.get("data")
    if not url:
        raise InstallerError("CurseForge did not provide a download URL for this file.")
    return url


def read_curseforge_api_key() -> str:
    if not CONFIG_FILE.is_file():
        try:
            with CONFIG_FILE.open("x", encoding="utf-8") as config:
                config.write("CF_API_KEY=your_api_key\n")
        except FileExistsError:
            pass
        except OSError as exc:
            raise InstallerError(f"Could not create {CONFIG_FILE.name}: {exc}") from exc
        else:
            raise InstallerError(
                f"Created {CONFIG_FILE.name}. Add your real API key as "
                "'CF_API_KEY=your_api_key' and run the installer again."
            )
    try:
        lines = CONFIG_FILE.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise InstallerError(f"Could not read {CONFIG_FILE.name}: {exc}") from exc

    values = [
        line.split("=", 1)[1].strip().strip("\"'")
        for line in lines
        if "=" in line and line.split("=", 1)[0].strip() == "CF_API_KEY"
    ]
    if len(values) != 1 or not values[0] or values[0] == "your_api_key":
        raise InstallerError(
            f"{CONFIG_FILE.name} must contain your real API key as "
            "'CF_API_KEY=your_api_key'."
        )
    return values[0]


def install_curseforge(project_id: str, file_id: str | None, api_key: str):
    file_data, headers = curseforge_file(project_id, file_id, api_key)
    if file_data.get("isServerPack"):
        download_url = curseforge_download_url(
            project_id, int(file_data["id"]), file_data, headers
        )
        archive = SERVER_DIR / "server-pack.zip"
        download(download_url, archive)
        safe_extract(archive, SERVER_DIR)
        archive.unlink()
        return file_data

    download_url = curseforge_download_url(
        project_id, int(file_data["id"]), file_data, headers
    )
    archive = SERVER_DIR / "client-pack.zip"
    download(download_url, archive)
    safe_extract(archive, SERVER_DIR)
    archive.unlink()

    manifest_path = SERVER_DIR / "manifest.json"
    if not manifest_path.is_file():
        raise InstallerError(
            "The selected CurseForge file is not a server pack or a valid "
            "modpack archive (manifest.json is missing)."
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallerError("The CurseForge modpack has an invalid manifest.json.") from exc
    if not isinstance(manifest.get("files"), list):
        raise InstallerError("The CurseForge modpack manifest has no valid files list.")

    for mod in manifest["files"]:
        if not isinstance(mod, dict) or mod.get("required", True) is False:
            continue
        mod_id = mod.get("projectID")
        mod_file_id = mod.get("fileID")
        if not isinstance(mod_id, int) or not isinstance(mod_file_id, int):
            raise InstallerError("A required mod entry has invalid projectID or fileID.")
        detail = get_json(
            f"https://api.curseforge.com/v1/mods/{mod_id}/files/{mod_file_id}",
            headers,
        ).get("data")
        if not detail:
            raise InstallerError(
                f"CurseForge did not return details for required mod file {mod_file_id}."
            )
        mod_url = curseforge_download_url(mod_id, mod_file_id, detail, headers)
        filename = detail.get("fileName")
        if not filename or Path(filename).name != filename:
            raise InstallerError(
                f"CurseForge returned an invalid filename for mod file {mod_file_id}."
            )
        download(mod_url, SERVER_DIR / "mods" / filename)
    return file_data


def modrinth_project(project_id: str) -> dict:
    project = get_json(
        f"https://api.modrinth.com/v2/project/{urllib.parse.quote(project_id)}"
    )
    if not project.get("id") or not project.get("title"):
        raise InstallerError("Modrinth did not return a valid modpack name.")
    return project


def modrinth_version(project: dict, version_id: str | None):
    canonical_id = project["id"]
    if version_id:
        version = get_json(f"https://api.modrinth.com/v2/version/{version_id}")
        if version.get("project_id") != canonical_id:
            raise InstallerError("That Modrinth version ID does not belong to this project.")
        return version
    versions = get_json(
        f"https://api.modrinth.com/v2/project/{canonical_id}/version"
    )
    if not versions:
        raise InstallerError("No versions were found for that Modrinth project.")
    return versions[0]


def install_modrinth(version: dict):
    pack_file = next(
        (entry for entry in version.get("files", []) if entry.get("primary")),
        (version.get("files") or [None])[0],
    )
    if not pack_file or not pack_file.get("url"):
        raise InstallerError("The selected Modrinth version has no downloadable file.")

    archive = SERVER_DIR / "modrinth-pack.mrpack"
    download(pack_file["url"], archive)
    try:
        with zipfile.ZipFile(archive) as zf:
            index = json.loads(zf.read("modrinth.index.json"))
            safe_extract(archive, SERVER_DIR)
    except (KeyError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        raise InstallerError("Downloaded file is not a valid Modrinth modpack.") from exc
    archive.unlink()

    server_overrides = SERVER_DIR / "server-overrides"
    overrides = SERVER_DIR / "overrides"
    for source in (overrides, server_overrides):
        if source.is_dir():
            shutil.copytree(source, SERVER_DIR, dirs_exist_ok=True)

    for entry in index.get("files", []):
        if entry.get("env", {}).get("server") == "unsupported":
            continue
        downloads = entry.get("downloads") or []
        if not downloads:
            raise InstallerError(
                f"Modrinth pack file {entry.get('path', '(unknown)')} has no download URL."
            )
        relative = PurePosixPath(entry["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise InstallerError(f"Unsafe file path in Modrinth index: {entry['path']}")
        destination = SERVER_DIR.joinpath(*relative.parts)
        if not destination.exists():
            download(downloads[0], destination)

    shutil.rmtree(overrides, ignore_errors=True)
    shutil.rmtree(server_overrides, ignore_errors=True)
    return version


def safe_directory_name(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", name).strip(" .")
    if not cleaned or cleaned in {".", ".."}:
        raise InstallerError("The modpack name cannot be used as a directory name.")
    if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", cleaned, re.IGNORECASE):
        cleaned = f"_{cleaned}"
    return cleaned


def is_matching_modrinth_partial_install(
    project: dict, version: dict, directory: Path
) -> bool:
    index_path = directory / "modrinth.index.json"
    if not index_path.is_file():
        return False
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if (
        index.get("name") != project.get("title")
        or index.get("versionId") != version.get("version_number")
        or not isinstance(index.get("files"), list)
    ):
        return False
    if any(
        (directory / name).exists()
        for name in (
            ".runtime",
            "eula.txt",
            "start.sh",
            "run.sh",
            "server.jar",
            "fabric-server-launch.jar",
            "quilt-server-launch.jar",
        )
    ):
        return False

    expected_top_level = {
        PurePosixPath(entry["path"]).parts[0]
        for entry in index["files"]
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("path"), str)
            and PurePosixPath(entry["path"]).parts
            and not PurePosixPath(entry["path"]).is_absolute()
            and ".." not in PurePosixPath(entry["path"]).parts
        )
    }
    expected_top_level.update(
        {"modrinth.index.json", "icon.png", "instance.png", "overrides", "config"}
    )
    return all(path.name in expected_top_level for path in directory.iterdir())


def server_version(provider: str, metadata: dict) -> tuple[str, str | None, str | None]:
    if provider == "modrinth":
        dependencies = metadata.get("dependencies") or {}
        game_version = (
            dependencies.get("minecraft")
            if isinstance(dependencies, dict)
            else None
        )
        if not game_version:
            game_versions = metadata.get("game_versions") or []
            game_version = next(
                (value for value in game_versions
                 if re.fullmatch(r"\d+\.\d+(?:\.\d+)?", value)),
                None,
            )
        loader_keys = (
            ("neoforge", "neoforge"),
            ("forge", "forge"),
            ("fabric", "fabric"),
            ("fabric-loader", "fabric"),
            ("quilt", "quilt"),
            ("quilt-loader", "quilt"),
        )
        loader, loader_version = next(
            (
                (normalized, dependencies[key])
                for key, normalized in loader_keys
                if isinstance(dependencies, dict) and dependencies.get(key)
            ),
            (None, None),
        )
        if not loader:
            supported = {"neoforge", "forge", "fabric", "quilt"}
            loader = next(
                (name for name in metadata.get("loaders", []) if name in supported),
                None,
            )
    else:
        manifest_path = SERVER_DIR / "manifest.json"
        manifest = {}
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise InstallerError("The server pack contains an invalid manifest.json.") from exc
        game_version = manifest.get("minecraft", {}).get("version")
        loaders = manifest.get("minecraft", {}).get("modLoaders") or []
        loader, loader_version = None, None
        if loaders:
            raw = loaders[0].get("id", "")
            for candidate in ("neoforge", "fabric", "quilt", "forge"):
                if raw.lower().startswith(candidate):
                    loader = candidate
                    raw_version = raw[len(candidate):].lstrip("-")
                    loader_version = (
                        raw_version.split("-", 1)[0]
                        if candidate in {"fabric", "quilt"}
                        else raw_version
                    )
                    break
        if not game_version:
            versions = metadata.get("gameVersions") or []
            game_version = next(
                (value for value in versions if re.fullmatch(r"\d+\.\d+(?:\.\d+)?", value)),
                None,
            )

    if not game_version:
        raise InstallerError(
            "Could not detect the Minecraft version from this pack; refusing to guess."
        )
    return game_version, loader, loader_version


def resolve_loader_version(loader: str, game_version: str) -> str:
    if loader == "fabric":
        versions = get_json(
            f"https://meta.fabricmc.net/v2/versions/loader/{game_version}"
        )
        version = next(
            (
                entry.get("loader", {}).get("version")
                for entry in versions
                if entry.get("loader", {}).get("stable")
            ),
            None,
        )
        if not version and versions:
            version = versions[0].get("loader", {}).get("version")
    elif loader == "quilt":
        versions = get_json(
            f"https://meta.quiltmc.org/v3/versions/loader/{game_version}"
        )
        version = next(
            (
                entry.get("loader", {}).get("version")
                for entry in versions
                if entry.get("loader", {}).get("stable")
            ),
            None,
        )
        if not version and versions:
            version = versions[0].get("loader", {}).get("version")
    elif loader == "forge":
        promotions = get_json(
            "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
        ).get("promos", {})
        version = promotions.get(f"{game_version}-recommended") or promotions.get(
            f"{game_version}-latest"
        )
    elif loader == "neoforge":
        version_xml = request(
            "https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml"
        )
        try:
            root = ET.fromstring(version_xml)
            version_list = root.find("./versioning/versions")
            versions = [
                item.text
                for item in (version_list or [])
                if item.text and re.match(r"^\d+\.\d+\.\d+$", item.text)
            ]
        except ET.ParseError as exc:
            raise InstallerError("NeoForge returned invalid version metadata.") from exc
        match = re.match(r"^\d+\.(\d+)", game_version)
        compatible_prefix = f"21.{match.group(1)}." if match else ""
        compatible_versions = [
            value for value in versions if value.startswith(compatible_prefix)
        ]
        version = max(
            compatible_versions,
            key=lambda value: tuple(int(part) for part in value.split(".")),
            default=None,
        )
    else:
        raise InstallerError(f"Unsupported mod loader: {loader}")

    if not version:
        raise InstallerError(
            f"Could not find a {loader} server version compatible with Minecraft "
            f"{game_version}."
        )
    return version


def minecraft_java_major(game_version: str) -> int:
    manifest = get_json(
        "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
    )
    entry = next((item for item in manifest.get("versions", [])
                  if item.get("id") == game_version), None)
    if not entry:
        raise InstallerError(f"Mojang has no version metadata for Minecraft {game_version}.")
    version_metadata = get_json(entry["url"])
    major = version_metadata.get("javaVersion", {}).get("majorVersion")
    if type(major) is int:
        return major

    match = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", game_version)
    if not match:
        raise InstallerError(f"Could not determine the Java version for {game_version}.")
    minecraft_major, minor, patch = (int(value or 0) for value in match.groups())
    if minecraft_major > 1 or (minecraft_major == 1 and minor >= 22):
        return 25
    if minecraft_major == 1 and minor == 21:
        return 21
    if minecraft_major == 1 and minor == 20 and patch >= 5:
        return 21
    if minecraft_major == 1 and minor >= 18:
        return 17
    if minecraft_major == 1 and minor == 17:
        return 16
    return 8


def temurin_platform() -> tuple[str, str]:
    operating_system = {
        "Linux": "linux",
        "Windows": "windows",
        "Darwin": "mac",
    }.get(platform.system())
    architecture = {
        "x86_64": "x64",
        "AMD64": "x64",
        "aarch64": "aarch64",
        "arm64": "aarch64",
        "x86": "x32",
        "i386": "x32",
    }.get(platform.machine())
    if not operating_system or not architecture:
        raise InstallerError(
            f"Temurin JDK downloads are not configured for "
            f"{platform.system()} {platform.machine()}."
        )
    return operating_system, architecture


def safe_extract_tar(archive: Path, target: Path):
    target = target.resolve()
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                output = (target / member.name).resolve()
                if output != target and target not in output.parents:
                    raise InstallerError(
                        f"Unsafe path in Temurin archive: {member.name}"
                    )
                if member.issym():
                    link_target = (output.parent / member.linkname).resolve()
                    if link_target != target and target not in link_target.parents:
                        raise InstallerError(
                            f"Unsafe symbolic link in Temurin archive: {member.name}"
                        )
                elif member.islnk():
                    link_target = (target / member.linkname).resolve()
                    if link_target != target and target not in link_target.parents:
                        raise InstallerError(
                            f"Unsafe hard link in Temurin archive: {member.name}"
                        )
                elif not (member.isdir() or member.isfile()):
                    raise InstallerError(
                        f"Unsupported entry in Temurin archive: {member.name}"
                    )
            tar.extractall(target, filter="data")
    except (tarfile.TarError, OSError, ValueError) as exc:
        raise InstallerError(f"Could not extract Temurin JDK: {exc}") from exc


def install_temurin(java_major: int) -> tuple[Path, Path]:
    operating_system, architecture = temurin_platform()
    archive_name = "temurin-jdk.zip" if operating_system == "windows" else "temurin-jdk.tar.gz"
    archive = SERVER_DIR / archive_name
    runtime_dir = SERVER_DIR / ".runtime"
    url = (
        f"https://api.adoptium.net/v3/binary/latest/{java_major}/ga/"
        f"{operating_system}/{architecture}/jdk/hotspot/normal/eclipse"
    )
    download(url, archive)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    if operating_system == "windows":
        safe_extract(archive, runtime_dir)
    else:
        safe_extract_tar(archive, runtime_dir)
    archive.unlink()

    java_name = "java.exe" if operating_system == "windows" else "java"
    matches = list(runtime_dir.rglob(java_name))
    java_executables = [
        path for path in matches if path.parent.name == "bin" and path.is_file()
    ]
    if not java_executables:
        raise InstallerError(
            f"Could not find the Temurin Java {java_major} executable."
        )
    shallowest_depth = min(
        len(path.relative_to(runtime_dir).parts) for path in java_executables
    )
    java_executables = [
        path for path in java_executables
        if len(path.relative_to(runtime_dir).parts) == shallowest_depth
    ]
    if len(java_executables) != 1:
        raise InstallerError(
            f"Expected one Temurin JDK {java_major} executable, "
            f"found {len(java_executables)}."
        )
    java_executable = java_executables[0]
    if operating_system != "windows":
        java_executable.chmod(java_executable.stat().st_mode | 0o111)
    result = subprocess.run(
        [str(java_executable), "-version"],
        capture_output=True,
        text=True,
        check=False,
    )
    version_output = result.stdout + result.stderr
    version_match = re.search(r'version "([^"]+)"', version_output)
    if result.returncode or not version_match:
        raise InstallerError(
            f"Downloaded Temurin JDK {java_major} could not be started:\n"
            f"{version_output.strip()}"
        )
    version_parts = version_match.group(1).split(".")
    try:
        detected_major = int(
            version_parts[1] if version_parts[0] == "1" else version_parts[0]
        )
    except (IndexError, ValueError) as exc:
        raise InstallerError(
            f"Could not parse the Temurin Java version: {version_match.group(1)}"
        ) from exc
    if detected_major != java_major:
        raise InstallerError(
            f"Downloaded Temurin Java {detected_major}, but Java {java_major} was requested."
        )
    return java_executable, java_executable.parent.parent


def java_environment(java_executable: Path, java_home: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["JAVA_HOME"] = str(java_home)
    environment["JAVA"] = str(java_executable)
    environment["PATH"] = str(java_executable.parent) + os.pathsep + environment.get("PATH", "")
    return environment


def download_vanilla_server(game_version: str):
    manifest = get_json("https://piston-meta.mojang.com/mc/game/version_manifest_v2.json")
    entry = next((v for v in manifest.get("versions", []) if v["id"] == game_version), None)
    if not entry:
        raise InstallerError(f"Mojang has no server metadata for Minecraft {game_version}.")
    version_data = get_json(entry["url"])
    server_url = version_data.get("downloads", {}).get("server", {}).get("url")
    if not server_url:
        raise InstallerError(f"Minecraft {game_version} has no downloadable server jar.")
    download(server_url, SERVER_DIR / "server.jar")


def install_loader(
    game_version: str,
    loader: str,
    loader_version: str,
    java_executable: Path,
    java_home: Path,
):
    java = str(java_executable)
    environment = java_environment(java_executable, java_home)
    installer = SERVER_DIR / "loader-installer.jar"

    if loader == "fabric":
        metadata = get_json("https://meta.fabricmc.net/v2/versions/installer")
        installer_version = next((v["version"] for v in metadata if v.get("stable")), None)
        if not installer_version:
            raise InstallerError("Could not find a stable Fabric installer.")
        url = (
            "https://maven.fabricmc.net/net/fabricmc/fabric-installer/"
            f"{installer_version}/fabric-installer-{installer_version}.jar"
        )
        download(url, installer)
        command = [java, "-jar", str(installer), "server", "-mcversion", game_version,
                   "-loader", loader_version, "-downloadMinecraft"]
    elif loader == "forge":
        forge_version = loader_version
        coordinate = f"{game_version}-{forge_version}"
        url = (
            "https://maven.minecraftforge.net/net/minecraftforge/forge/"
            f"{coordinate}/forge-{coordinate}-installer.jar"
        )
        download(url, installer)
        command = [java, "-jar", str(installer), "--installServer"]
    elif loader == "quilt":
        metadata = get_json("https://meta.quiltmc.org/v3/versions/installer")
        installer_version = metadata[0]["version"] if metadata else None
        if not installer_version:
            raise InstallerError("Could not find a Quilt installer.")
        url = (
            "https://maven.quiltmc.org/repository/release/org/quiltmc/"
            f"quilt-installer/{installer_version}/quilt-installer-{installer_version}.jar"
        )
        download(url, installer)
        command = [java, "-jar", str(installer), "install", "server",
                   game_version, loader_version, "--install-dir", str(SERVER_DIR)]
    elif loader == "neoforge":
        version = loader_version
        url = (
            "https://maven.neoforged.net/releases/net/neoforged/neoforge/"
            f"{version}/neoforge-{version}-installer.jar"
        )
        download(url, installer)
        command = [java, "-jar", str(installer), "--installServer"]
    else:
        raise InstallerError(f"Unsupported mod loader: {loader}")

    print(f"Installing {loader} for Minecraft {game_version}...")
    result = subprocess.run(command, cwd=SERVER_DIR, env=environment, check=False)
    if result.returncode:
        raise InstallerError(f"{loader} installer failed with exit code {result.returncode}.")
    installer.unlink(missing_ok=True)


def has_server_runtime() -> bool:
    return any(
        (SERVER_DIR / name).is_file()
        for name in (
            "run.sh",
            "start.sh",
            "startserver.sh",
            "ServerStart.sh",
            "launch.sh",
            "server.jar",
            "fabric-server-launch.jar",
            "quilt-server-launch.jar",
        )
    ) or any(SERVER_DIR.glob("minecraft_server*.jar"))


def server_command(java_executable: Path) -> list[str]:
    java = str(java_executable)
    for script in (
        "run.sh",
        "pack-start.sh",
        "startserver.sh",
        "ServerStart.sh",
        "launch.sh",
    ):
        path = SERVER_DIR / script
        if path.is_file():
            path.chmod(path.stat().st_mode | 0o111)
            return ["bash", path.name, "nogui"]
    for jar in ("fabric-server-launch.jar", "quilt-server-launch.jar", "server.jar"):
        if (SERVER_DIR / jar).is_file():
            return [java, "-jar", jar, "nogui"]
    server_jar = next(SERVER_DIR.glob("minecraft_server*.jar"), None)
    if server_jar:
        return [java, "-jar", server_jar.name, "nogui"]
    raise InstallerError(
        "No runnable server was produced. Check the pack's server files and "
        "modloader support."
    )


def create_start_script(command: list[str], java_executable: Path, java_home: Path):
    relative_java_home = java_home.relative_to(SERVER_DIR).as_posix()
    java_binary = java_executable.name
    quoted = " ".join("'" + arg.replace("'", "'\"'\"'") + "'" for arg in command)
    if Path(command[0]) == java_executable:
        args = " ".join("'" + arg.replace("'", "'\"'\"'") + "'" for arg in command[1:])
        quoted = f'"$JAVA" {args}'.rstrip()
    script = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"\n'
        f'export JAVA_HOME="$SCRIPT_DIR/{relative_java_home}"\n'
        f'export JAVA="$JAVA_HOME/bin/{java_binary}"\n'
        'export PATH="$JAVA_HOME/bin:$PATH"\n'
        'cd -- "$SCRIPT_DIR"\n'
        f"exec {quoted} \"$@\"\n"
    )
    path = SERVER_DIR / "start.sh"
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)


def test_server(command: list[str], java_executable: Path, java_home: Path):
    eula_path = SERVER_DIR / "eula.txt"
    eula_accepted = eula_path.exists() and re.search(
        r"(?im)^\s*eula\s*=\s*true\s*$", eula_path.read_text(encoding="utf-8")
    )
    if not eula_accepted:
        answer = input(
            "Starting the server requires agreeing to the Minecraft EULA "
            "(https://aka.ms/MinecraftEULA). Do you agree? [y/N] "
        ).strip().lower()
        if answer not in {"y", "yes"}:
            raise InstallerError(
                "EULA not accepted. start.sh was created, but the server was not run."
            )
        eula_path.write_text("eula=true\n", encoding="utf-8")

    print("Running a startup test (up to 180 seconds)...")
    output_lines = []
    process = subprocess.Popen(
        command,
        cwd=SERVER_DIR,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        start_new_session=(os.name == "posix"),
        env=java_environment(java_executable, java_home),
    )
    if process.stdout is None:
        raise InstallerError("Could not capture server startup logs.")

    startup_ready = threading.Event()

    def stream_logs():
        for line in process.stdout:
            output_lines.append(line)
            print(line, end="", flush=True)
            if "Done (" in line or "For help, type" in line:
                startup_ready.set()

    log_thread = threading.Thread(target=stream_logs, daemon=True)
    log_thread.start()
    deadline = time.monotonic() + 180
    try:
        while (
            process.poll() is None
            and not startup_ready.is_set()
            and time.monotonic() < deadline
        ):
            startup_ready.wait(timeout=0.2)
        if process.poll() is None and process.stdin:
            if startup_ready.is_set():
                print("Server is ready; stopping the test server...")
            try:
                process.stdin.write("stop\n")
                process.stdin.flush()
                process.stdin.close()
            except BrokenPipeError:
                pass
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                terminate_process(process)
                process.wait(timeout=10)
        log_thread.join(timeout=10)
        output = "".join(output_lines)
    finally:
        if process.poll() is None:
            terminate_process(process)
            process.wait(timeout=10)
        if process.stdout:
            process.stdout.close()
        log_thread.join(timeout=10)

    if process.returncode not in (0, None):
        tail = "\n".join(output.splitlines()[-25:])
        raise InstallerError(
            f"Server startup test failed (exit code {process.returncode}).\n{tail}"
        )
    if "Done (" not in output and "For help, type" not in output:
        tail = "\n".join(output.splitlines()[-25:])
        raise InstallerError(f"Server did not report a successful startup.\n{tail}")


def terminate_process(process: subprocess.Popen):
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    else:
        process.terminate()


def find_installed_modpacks(root: Path) -> list[Path]:
    root = root.resolve()
    return sorted(
        (
            path for path in root.iterdir()
            if path.is_dir()
            and not path.is_symlink()
            and (path / "start.sh").is_file()
        ),
        key=lambda path: path.name.casefold(),
    )


def uninstall_modpack(root: Path):
    modpacks = find_installed_modpacks(root)
    if not modpacks:
        print("No installed modpacks found in this folder.")
        return

    print("Installed modpacks:")
    for index, modpack in enumerate(modpacks, start=1):
        print(f"{index}) {modpack.name}")
    selection = input("Enter the number of the modpack to uninstall: ").strip()
    if not selection.isdecimal() or not 1 <= int(selection) <= len(modpacks):
        raise InstallerError("Select a number from the installed modpack list.")

    selected = modpacks[int(selection) - 1]
    print(f"This will permanently delete '{selected.name}' and all its files.")
    confirmation = input(f"Type {selected.name} to confirm: ").strip()
    if confirmation != selected.name:
        print("Uninstall cancelled.")
        return

    root = root.resolve()
    target = selected.resolve()
    if (
        target.parent != root
        or target.is_symlink()
        or not target.is_dir()
        or not (target / "start.sh").is_file()
    ):
        raise InstallerError("The selected modpack folder changed; refusing to delete it.")
    shutil.rmtree(target)
    print(f"Uninstalled {selected.name}.")


def main():
    global SERVER_DIR

    print("Minecraft modpack server installer")
    print("1) Install a modpack")
    print("2) Uninstall a modpack")
    action = input("Choose an action [1/2]: ").strip()
    if action == "2":
        uninstall_modpack(Path.cwd())
        return
    if action != "1":
        raise InstallerError("Choose 1 to install or 2 to uninstall a modpack.")

    print("1) CurseForge")
    print("2) Modrinth")
    choice = input("Choose a platform [1/2]: ").strip()
    if choice not in {"1", "2"}:
        raise InstallerError("Choose 1 for CurseForge or 2 for Modrinth.")
    project_id = input("Enter project ID: ").strip()
    if not project_id:
        raise InstallerError("Project ID cannot be empty.")
    file_id = input("Enter file/version ID (optional; press Enter for latest): ").strip() or None

    provider = "curseforge" if choice == "1" else "modrinth"
    version = None
    if provider == "curseforge":
        api_key = read_curseforge_api_key()
        project = curseforge_project(project_id, api_key)
        pack_name = project["name"]
    else:
        project = modrinth_project(project_id)
        pack_name = project["title"]
        version = modrinth_version(project, file_id)

    SERVER_DIR = Path.cwd() / safe_directory_name(pack_name)
    existing_contents = SERVER_DIR.exists() and any(SERVER_DIR.iterdir())
    can_resume = (
        provider == "modrinth"
        and existing_contents
        and version is not None
        and is_matching_modrinth_partial_install(project, version, SERVER_DIR)
    ) if provider == "modrinth" else False
    if SERVER_DIR.exists() and (
        not SERVER_DIR.is_dir() or (existing_contents and not can_resume)
    ):
        raise InstallerError(
            f"{SERVER_DIR.name} already exists and is not empty; "
            "move it or choose another location."
        )
    SERVER_DIR.mkdir(parents=True, exist_ok=True)
    if can_resume:
        print(f"Resuming the matching partial installation in {SERVER_DIR.name}...")

    if provider == "curseforge":
        metadata = install_curseforge(project_id, file_id, api_key)
    else:
        if version is None:
            raise InstallerError("Could not select a Modrinth modpack version.")
        metadata = install_modrinth(version)

    game_version, loader, loader_version = server_version(provider, metadata)
    mods_dir = SERVER_DIR / "mods"
    if loader is None and mods_dir.is_dir() and any(mods_dir.glob("*.jar")):
        raise InstallerError(
            "The modpack contains mods but does not declare a supported server "
            "modloader, so a compatible server cannot be generated automatically."
        )
    print(f"Detected Minecraft {game_version}" +
          (f" with {loader} {loader_version}" if loader else " (vanilla)"))

    java_major = minecraft_java_major(game_version)
    print(f"Installing Temurin JDK {java_major} for Minecraft {game_version}...")
    java_executable, java_home = install_temurin(java_major)

    if loader:
        if not has_server_runtime():
            if not loader_version:
                loader_version = resolve_loader_version(loader, game_version)
            install_loader(
                game_version,
                loader,
                loader_version,
                java_executable,
                java_home,
            )
    elif not has_server_runtime():
        download_vanilla_server(game_version)

    if (SERVER_DIR / "start.sh").is_file():
        (SERVER_DIR / "start.sh").replace(SERVER_DIR / "pack-start.sh")
    command = server_command(java_executable)
    create_start_script(command, java_executable, java_home)
    test_server(command, java_executable, java_home)
    print("Okay — the server passed its startup test.")
    print(f"Start it later with: cd \"{SERVER_DIR.name}\" && ./start.sh")


if __name__ == "__main__":
    try:
        main()
    except (InstallerError, urllib.error.HTTPError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
