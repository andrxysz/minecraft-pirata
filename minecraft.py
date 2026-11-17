from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path


APP_NAME = "Mine Pirata"
APP_DIR = Path(os.environ.get("APPDATA", Path.home() / ".config")) / APP_NAME
GAME_DIR = Path(os.environ.get("APPDATA", Path.home())) / ".minecraft"
OS_NAME = {"Windows": "windows", "Darwin": "osx"}.get(platform.system(), "linux")
ARCH = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else (
    "x86_64" if platform.architecture()[0] == "64bit" else "x86"
)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class LaunchError(Exception):
    """Problema que pode ser apresentado diretamente na interface."""


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("o conteúdo deve ser um objeto JSON")
        return value
    except (OSError, ValueError) as error:
        raise LaunchError(f"Não foi possível ler {path.name}: {error}") from error


def load_settings(app_dir: Path = APP_DIR) -> dict:
    try:
        return read_json(app_dir / "settings.json")
    except LaunchError:
        return {}


def save_settings(settings: dict, app_dir: Path = APP_DIR) -> None:
    app_dir.mkdir(parents=True, exist_ok=True)
    temporary = app_dir / "settings.json.tmp"
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(app_dir / "settings.json")


def inside(root: Path, relative: str) -> Path:
    result = (root / relative).resolve()
    if not result.is_relative_to(root.resolve()):
        raise LaunchError(f"Caminho fora da pasta esperada: {relative}")
    return result


def version_file(root: Path, version: str) -> Path:
    if not version or version in (".", "..") or any(c in version for c in '/\\:'):
        raise LaunchError("Identificador de versão inválido.")
    return inside(root / "versions", f"{version}/{version}.json")


def installed_versions(root: Path = GAME_DIR) -> list[str]:
    directory = root / "versions"
    if not directory.is_dir():
        return []
    found = [(p.stat().st_mtime, p.parent.name) for p in directory.glob("*/*.json")
             if p.stem == p.parent.name]
    return [name for _, name in sorted(found, reverse=True)]


def library_key(library: dict) -> tuple:
    parts = library["name"].split(":")
    return tuple(parts[:2] + parts[3:])


def resolve_version(root: Path, version: str, seen: tuple = ()) -> dict:
    if version in seen or len(seen) >= 20:
        raise LaunchError("Herança circular ou excessiva no manifesto da versão.")
    child = read_json(version_file(root, version))
    parent_id = child.get("inheritsFrom")
    if not parent_id:
        return {**child, "_client": child.get("jar", version)}
    parent = resolve_version(root, parent_id, (*seen, version))
    merged = {**parent, **child, "_client": child.get("jar", parent["_client"])}
    arguments = {}
    for key in set(parent.get("arguments", {})) | set(child.get("arguments", {})):
        arguments[key] = parent.get("arguments", {}).get(key, []) + child.get("arguments", {}).get(key, [])
    if arguments:
        merged["arguments"] = arguments
    libraries = {}
    for library in child.get("libraries", []) + parent.get("libraries", []):
        libraries.setdefault(library_key(library), library)
    merged["libraries"] = list(libraries.values())
    return merged


def rules_allow(item: dict) -> bool:
    rules = item.get("rules")
    if rules is None:
        return True
    allowed = False
    aliases = {"amd64": "x86_64", "aarch64": "arm64"}
    for rule in rules:
        system = rule.get("os", {})
        if system.get("name", OS_NAME) != OS_NAME:
            continue
        arch = system.get("arch", ARCH)
        if aliases.get(arch, arch) != ARCH:
            continue
        if "version" in system and not re.search(system["version"], platform.version()):
            continue
        if any(bool(value) for value in rule.get("features", {}).values()):
            continue
        allowed = rule.get("action") == "allow"
    return allowed


def maven_path(name: str) -> str:
    coordinate, _, extension = name.partition("@")
    parts = coordinate.split(":")
    if len(parts) not in (3, 4):
        raise LaunchError(f"Biblioteca com nome inválido: {name}")
    group, artifact, version = parts[:3]
    classifier = "-" + parts[3] if len(parts) == 4 else ""
    return f"{group.replace('.', '/')}/{artifact}/{version}/{artifact}-{version}{classifier}.{extension or 'jar'}"


def library_entries(root: Path, manifest: dict) -> tuple[list[tuple[Path, dict]], list[tuple[Path, dict, list[str]]]]:
    """Os mesmos caminhos e regras para instalar e para construir o comando."""
    artifacts, natives = [], []
    library_root = root / "libraries"
    for library in manifest.get("libraries", []):
        if not rules_allow(library):
            continue
        downloads = library.get("downloads", {})
        artifact = downloads.get("artifact")
        # Maven
        if artifact is not None or not downloads:
            path = inside(library_root, (artifact or {}).get("path") or maven_path(library["name"]))
            spec = dict(artifact or {})
            spec.setdefault("url", library.get("url", "https://libraries.minecraft.net/").rstrip("/") + "/" + maven_path(library["name"]))
            artifacts.append((path, spec))
        classifier = library.get("natives", {}).get(OS_NAME)
        if classifier:
            classifier = classifier.replace("${arch}", "64" if ARCH != "x86" else "32")
            native = downloads.get("classifiers", {}).get(classifier)
            if native is None:
                raise LaunchError(f"Natives não definidos para {library['name']} ({classifier}).")
            native_name = library["name"] + ":" + classifier
            path = inside(library_root, native.get("path") or maven_path(native_name))
            natives.append((path, native, library.get("extract", {}).get("exclude", ["META-INF/"])))
    return artifacts, natives


def library_files(root: Path, manifest: dict) -> tuple[list[Path], list[tuple[Path, list[str]]]]:
    artifacts, native_entries = library_entries(root, manifest)
    classpath = [path for path, _ in artifacts]
    natives = [(path, excluded) for path, _, excluded in native_entries]
    missing = [path.name for path in classpath + [p for p, _ in natives] if not path.is_file()]
    if missing:
        sample = "\n".join(missing[:5])
        raise LaunchError(f"Faltam {len(missing)} bibliotecas nesta instalação:\n{sample}\n\nRepare a versão no launcher que a instalou.")
    return list(dict.fromkeys(classpath)), natives


def java_major(executable: Path) -> int | None:
    release = executable.parent.parent / "release"
    try:
        text = release.read_text(encoding="utf-8")
    except OSError:
        try:
            result = subprocess.run([str(executable), "-version"], capture_output=True,
                                    text=True, timeout=5, creationflags=NO_WINDOW)
            text = result.stdout + result.stderr
        except (OSError, subprocess.SubprocessError):
            return None
    match = re.search(r'(?:JAVA_VERSION=|version\s+)"(\d+)(?:\.(\d+))?', text)
    if not match:
        return None
    return int(match[2]) if match[1] == "1" and match[2] else int(match[1])


def find_java(root: Path, required: int, override: str = "") -> Path:
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_file():
            raise LaunchError(f"Java configurado não encontrado: {candidate}")
        if java_major(candidate) != required:
            raise LaunchError(f"Esta versão precisa do Java {required}. Confira java_path em settings.json.")
        return candidate
    executable = "java.exe" if OS_NAME == "windows" else "java"
    candidates = []
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        candidates.append(Path(java_home) / "bin" / executable)
    runtime = root / "runtime"
    if runtime.is_dir():
        candidates.extend(runtime.glob(f"**/bin/{executable}"))
    on_path = shutil.which("java")
    if on_path:
        candidates.append(Path(on_path))
    if OS_NAME == "windows":
        for variable in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(variable)
            if base:
                for vendor in ("Java", "Eclipse Adoptium", "Microsoft", "Zulu", "BellSoft"):
                    directory = Path(base) / vendor
                    if directory.is_dir():
                        candidates.extend(directory.glob(f"*/bin/{executable}"))
    for candidate in dict.fromkeys(candidates):
        if candidate.is_relative_to(runtime) and "blocklauncher" in candidate.relative_to(runtime).parts:
            if not (candidate.parent.parent / ".complete").is_file():
                continue
        if candidate.is_file() and java_major(candidate) == required:
            return candidate
    raise LaunchError(f"Java {required} não encontrado. Instale essa versão ou informe java_path em\n{APP_DIR / 'settings.json'}")


def offline_uuid(nickname: str) -> str:
    digest = bytearray(hashlib.md5(("OfflinePlayer:" + nickname).encode("utf-8")).digest())
    digest[6] = (digest[6] & 0x0F) | 0x30
    digest[8] = (digest[8] & 0x3F) | 0x80
    return digest.hex()


def expand(value: str, variables: dict) -> str:
    def replace(match):
        key = match[1]
        if key not in variables:
            raise LaunchError(f"Este perfil requer um argumento ainda não suportado: {key}")
        return str(variables[key])
    return re.sub(r"\$\{([^}]+)\}", replace, value)


def expand_arguments(arguments: list, variables: dict) -> list[str]:
    result = []
    for argument in arguments:
        if isinstance(argument, dict):
            if not rules_allow(argument):
                continue
            argument = argument["value"]
        values = argument if isinstance(argument, list) else [argument]
        result.extend(expand(value, variables) for value in values)
    return result


@dataclass
class LaunchPlan:
    command: list[str]
    game_dir: Path
    natives_dir: Path
    natives: list[tuple[Path, list[str]]]
    java_version: int
    version: str
    legacy_assets: tuple[Path, Path] | None = None


def build_plan(version: str, nickname: str, settings: dict | None = None,
               root: Path = GAME_DIR, app_dir: Path = APP_DIR) -> LaunchPlan:
    if not re.fullmatch(r"[A-Za-z0-9_]{3,16}", nickname):
        raise LaunchError("Use um nick de 3 a 16 caracteres: letras, números ou underscore (_).")
    settings = settings or {}
    manifest = resolve_version(root, version)
    main_class = manifest.get("mainClass")
    if not isinstance(main_class, str) or not main_class:
        raise LaunchError("A versão não declara uma classe principal (mainClass).")
    required = int(manifest.get("javaVersion", {}).get("majorVersion", 8))
    java = find_java(root, required, settings.get("java_path", ""))
    classpath, natives = library_files(root, manifest)
    client_version = manifest["_client"]
    version_file(root, client_version) 
    client = inside(root / "versions", f"{client_version}/{client_version}.jar")
    if not client.is_file():
        raise LaunchError(f"O arquivo do jogo não foi encontrado:\n{client}")
    classpath.append(client)
    asset_id = manifest.get("assetIndex", {}).get("id", manifest.get("assets", "legacy"))
    asset_index = inside(root / "assets" / "indexes", f"{asset_id}.json")
    if not asset_index.is_file():
        raise LaunchError(f"Índice de assets ausente: {asset_index.name}. Repare a instalação da versão.")
    assets = read_json(asset_index)
    natives_dir = inside(app_dir / "natives", version)
    game_assets = root / "assets"
    legacy_assets = None
    if assets.get("virtual") or assets.get("map_to_resources"):
        game_assets = root / "resources" if assets.get("map_to_resources") else root / "assets" / "virtual" / str(asset_id)
        legacy_assets = (asset_index, game_assets)
    variables = {
        "auth_player_name": nickname, "version_name": version,
        "game_directory": str(root.resolve()), "assets_root": str((root / "assets").resolve()),
        "game_assets": str(game_assets.resolve()), "assets_index_name": asset_id,
        "auth_uuid": offline_uuid(nickname), "auth_access_token": "0", "auth_session": "0",
        "user_type": "legacy", "user_properties": "{}", "profile_properties": "{}",
        "version_type": manifest.get("type", "release"), "natives_directory": str(natives_dir),
        "launcher_name": APP_NAME, "launcher_version": "1.0",
        "classpath": os.pathsep.join(str(p.resolve()) for p in classpath),
        "classpath_separator": os.pathsep, "library_directory": str((root / "libraries").resolve()),
        "clientid": "", "auth_xuid": "", "primary_jar": str(client.resolve()),
    }
    try:
        memory = int(settings.get("memory_mb", 2048))
    except (ValueError, TypeError) as error:
        raise LaunchError("memory_mb deve ser um número entre 512 e 16384.") from error
    if not 512 <= memory <= 16384:
        raise LaunchError("memory_mb deve ser um número entre 512 e 16384.")
    arguments = manifest.get("arguments", {})
    jvm = expand_arguments(arguments.get("jvm", []), variables)
    if not jvm:
        jvm = [f"-Djava.library.path={natives_dir}", "-cp", variables["classpath"]]
    logging = manifest.get("logging", {}).get("client", {})
    if logging.get("argument") and logging.get("file", {}).get("id"):
        log_config = inside(root / "assets" / "log_configs", logging["file"]["id"])
        if log_config.is_file():
            jvm.append(expand(logging["argument"], {**variables, "path": str(log_config)}))
    if "minecraftArguments" in manifest:
        game = [expand(value, variables) for value in shlex.split(manifest["minecraftArguments"])]
    else:
        game = expand_arguments(arguments.get("game", []), variables)
    if not game:
        raise LaunchError("A versão não declara os argumentos do jogo.")
    command = [str(java), "-Xms256M", f"-Xmx{memory}M", *jvm, main_class, *game]
    return LaunchPlan(command, root.resolve(), natives_dir, natives, required, version, legacy_assets)


def prepare_files(plan: LaunchPlan) -> None:
    plan.natives_dir.mkdir(parents=True, exist_ok=True)
    for archive, excluded in plan.natives:
        with zipfile.ZipFile(archive) as source:
            for entry in source.infolist():
                if entry.is_dir() or any(entry.filename.startswith(prefix) for prefix in excluded):
                    continue
                destination = inside(plan.natives_dir, entry.filename)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with source.open(entry) as src, destination.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
    if plan.legacy_assets:
        index, destination = plan.legacy_assets
        for name, item in read_json(index).get("objects", {}).items():
            digest = item["hash"]
            if not re.fullmatch(r"[0-9a-f]{40}", digest):
                raise LaunchError("Hash inválido no índice de assets.")
            source = plan.game_dir / "assets" / "objects" / digest[:2] / digest
            target = inside(destination, name)
            if not source.is_file():
                raise LaunchError(f"Asset ausente: {name}. Repare a instalação.")
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)


def start_game(plan: LaunchPlan, app_dir: Path = APP_DIR) -> subprocess.Popen:
    prepare_files(plan)
    logs = app_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / "game.log"
    # Arquivo temporário de argumentos evita o limite de 32 mil caracteres do Windows.
    command = plan.command
    if plan.java_version >= 9:
        arguments_file = app_dir / "launch.args"
        def quote(argument):
            return '"' + argument.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r') + '"'
        arguments_file.write_text("\n".join(quote(arg) for arg in command[1:]), encoding="utf-8")
        command = [command[0], "@" + str(arguments_file.resolve())]
    elif OS_NAME == "windows" and len(subprocess.list2cmdline(command)) > 32000:
        raise LaunchError("O comando desta versão excede o limite do Java 8 no Windows.")
    with log.open("w", encoding="utf-8") as output:
        output.write(f"{APP_NAME} | {plan.version} | Java {plan.java_version}\n")
        output.flush()
        return subprocess.Popen(command, cwd=plan.game_dir, stdin=subprocess.DEVNULL,
                                stdout=output, stderr=subprocess.STDOUT, creationflags=NO_WINDOW,
                                start_new_session=os.name != "nt")


def terminate_game(process: subprocess.Popen, timeout: float = 10) -> None:
    """Encerra somente o processo lançado pelo launcher e seus descendentes."""
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
            result = subprocess.run([str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                                    capture_output=True, text=True, errors="replace",
                                    timeout=timeout, creationflags=NO_WINDOW)
            if result.returncode and process.poll() is None:
                details = (result.stderr or result.stdout).strip()
                raise LaunchError(f"Não foi possível encerrar o Minecraft: {details}")
        else:
            if os.getpgid(process.pid) != process.pid:
                raise LaunchError("O jogo não pertence a um grupo de processos isolado.")
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=timeout)
    except ProcessLookupError:
        process.wait(timeout=timeout)
    except (OSError, subprocess.SubprocessError) as error:
        raise LaunchError(f"Não foi possível encerrar o Minecraft: {error}") from error
