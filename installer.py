"""Catálogo Mojang e instalação sob demanda, sem dependências externas."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import urllib.error
import urllib.request

from minecraft import (APP_DIR, GAME_DIR, ARCH, OS_NAME, LaunchError, find_java,
                       inside, library_entries, read_json, resolve_version, version_file)


CATALOG_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
RUNTIME_URL = "https://piston-meta.mojang.com/v1/products/java-runtime/2ec0cc96c44e5a76b9c8b7c39df7210883d12871/all.json"
ASSET_URL = "https://resources.download.minecraft.net/"
USER_AGENT = "BlockLauncher/1.2 (Python urllib)"


class InstallCancelled(LaunchError):
    pass


def check_cancel(cancel):
    if cancel is not None and cancel.is_set():
        raise InstallCancelled("Instalação cancelada.")


def request(url):
    if not isinstance(url, str) or not url.startswith("https://"):
        raise LaunchError(f"URL de download inválida: {url}")
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=20)


def get_json(url):
    try:
        with request(url) as response:
            data = json.load(response)
        if not isinstance(data, dict):
            raise ValueError("o conteúdo não é um objeto JSON")
        return data
    except (OSError, ValueError) as error:
        raise LaunchError(f"Não foi possível consultar o catálogo: {error}") from error


@dataclass
class Catalog:
    versions: dict[str, dict]
    latest_release: str = ""
    cached: bool = False


def parse_catalog(data, cached=False):
    versions = {}
    for entry in data.get("versions", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry.get("url"):
            raise LaunchError("O catálogo de versões contém uma entrada inválida.")
        versions[entry["id"]] = entry
    if not versions:
        raise LaunchError("O catálogo de versões está vazio.")
    return Catalog(versions, data.get("latest", {}).get("release", ""), cached)


def fetch_catalog(app_dir=APP_DIR, refresh=True):
    cache = app_dir / "cache" / "versions.json"
    cached = None
    try:
        cached = parse_catalog(read_json(cache), cached=True)
    except (LaunchError, KeyError, TypeError):
        pass
    if not refresh:
        return cached
    try:
        data = get_json(CATALOG_URL)
        result = parse_catalog(data)
    except (LaunchError, KeyError, TypeError):
        if cached:
            return cached
        raise
    # Falhar ao gravar o cache não impede que o catálogo online seja usado.
    temporary = None
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=cache.parent,
                                         suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            json.dump(data, output, ensure_ascii=False)
        temporary.replace(cache)
    except OSError:
        pass
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return result


def file_matches(path, spec, cancel=None):
    check_cancel(cancel)
    if not path.is_file():
        return False
    if spec.get("size") is not None and path.stat().st_size != spec["size"]:
        return False
    if spec.get("sha1"):
        digest = hashlib.sha1()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(256 * 1024), b""):
                check_cancel(cancel)
                digest.update(chunk)
        return digest.hexdigest() == spec["sha1"]
    return True


def download_file(path, spec, cancel=None, on_bytes=None):
    """Download atômico: mantém o arquivo anterior até validar tamanho e SHA-1."""
    if file_matches(path, spec, cancel):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(3):
        check_cancel(cancel)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".part", delete=False) as output:
                temporary = Path(output.name)
                digest, received = hashlib.sha1(), 0
                with request(spec.get("url")) as response:
                    while True:
                        check_cancel(cancel)
                        chunk = response.read(256 * 1024)
                        if not chunk:
                            break
                        output.write(chunk)
                        digest.update(chunk)
                        received += len(chunk)
                        if on_bytes:
                            on_bytes(received, spec.get("size", 0))
            if spec.get("size") is not None and received != spec["size"]:
                raise OSError("download incompleto (tamanho diferente do esperado)")
            if spec.get("sha1") and digest.hexdigest() != spec["sha1"]:
                raise OSError("o SHA-1 do arquivo não corresponde ao manifesto")
            check_cancel(cancel)
            temporary.replace(path)
            return True
        except InstallCancelled:
            raise
        except (OSError, ValueError) as error:
            if attempt == 2:
                raise LaunchError(f"Falha ao baixar {path.name}: {error}\nClique em Play para tentar novamente.") from error
            if cancel is not None:
                if cancel.wait(0.3 * (attempt + 1)):
                    check_cancel(cancel)
            else:
                time.sleep(0.3 * (attempt + 1))
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)


class Reporter:
    """Agrega progresso dos downloads e limita eventos enviados ao Tkinter."""
    def __init__(self, callback=None):
        self.callback = callback or (lambda *_: None)
        self.lock = threading.Lock()
        self.last = 0.0
        self.completed = 0
        self.total = 0

    def report(self, message, force=False):
        with self.lock:
            now = time.monotonic()
            if force or now - self.last >= 0.1:
                self.callback(message, self.completed, self.total)
                self.last = now

    def complete(self):
        with self.lock:
            self.completed += 1
        self.report(f"Verificando arquivos · {self.completed}/{self.total}", self.completed == self.total)


def run_downloads(jobs, reporter, cancel=None):
    jobs = list({path: spec for path, spec in jobs}.items())
    reporter.completed, reporter.total = 0, len(jobs)
    reporter.report(f"Verificando {len(jobs)} arquivos…", True)
    # Limita também os futures pendentes para catálogos grandes de assets.
    def work(job):
        path, spec = job
        def on_bytes(done, total):
            unit, divisor = ("MB", 1048576) if max(done, total or 0) >= 1048576 else ("KB", 1024)
            amount = f"{done / divisor:.1f}"
            if total:
                amount += f"/{total / divisor:.1f}"
            reporter.report(f"Baixando {spec.get('label', path.name)} · {amount} {unit}")
        download_file(path, spec, cancel, on_bytes)
        reporter.complete()
    with ThreadPoolExecutor(max_workers=6, thread_name_prefix="download") as executor:
        # Lotes evitam enfileirar milhares de trabalhos depois de um cancelamento.
        for offset in range(0, len(jobs), 36):
            check_cancel(cancel)
            list(executor.map(work, jobs[offset:offset + 36]))


def ensure_manifest(root, version, catalog, app_dir, reporter, cancel, seen=()):
    if version in seen or len(seen) >= 20:
        raise LaunchError("Herança circular ou excessiva no manifesto da versão.")
    path = version_file(root, version)
    if not path.is_file():
        if catalog is None:
            catalog = fetch_catalog(app_dir)
        entry = catalog.versions.get(version)
        if not entry:
            raise LaunchError(f"A versão {version} não está no catálogo oficial e não tem manifesto local.")
        reporter.report(f"Preparando Minecraft {version}…", True)
        download_file(path, entry, cancel)
    manifest = read_json(path)
    for parent in dict.fromkeys(filter(None, (manifest.get("inheritsFrom"), manifest.get("jar")))):
        if parent != version:
            ensure_manifest(root, parent, catalog, app_dir, reporter, cancel, (*seen, version))
    return manifest


def runtime_platform():
    if OS_NAME == "windows":
        return {"x86_64": "windows-x64", "x86": "windows-x86", "arm64": "windows-arm64"}[ARCH]
    if OS_NAME == "osx":
        return "mac-os-arm64" if ARCH == "arm64" else "mac-os"
    return "linux-i386" if ARCH == "x86" else "linux" if ARCH == "x86_64" else "linux-arm64"


def ensure_java(manifest, root, settings, reporter, cancel):
    required = int(manifest.get("javaVersion", {}).get("majorVersion", 8))
    try:
        return find_java(root, required, settings.get("java_path", ""))
    except LaunchError:
        # Uma escolha manual inválida deve ser corrigida pelo usuário.
        if settings.get("java_path"):
            raise
    check_cancel(cancel)
    reporter.report(f"Procurando o runtime do Java {required}…", True)
    index = get_json(RUNTIME_URL)
    component = manifest.get("javaVersion", {}).get("component", "jre-legacy")
    builds = index.get(runtime_platform(), {}).get(component, [])
    if not builds:
        raise LaunchError(f"Java {required} não disponível para este sistema. Instale-o e selecione nas configurações.")
    metadata = get_json(builds[0]["manifest"]["url"])
    directory = inside(root / "runtime" / "blocklauncher", f"{component}/{runtime_platform()}/{component}")
    jobs, executable_files, links = [], [], []
    for name, entry in metadata.get("files", {}).items():
        path = inside(directory, name)
        kind = entry.get("type")
        if kind == "file":
            jobs.append((path, entry["downloads"]["raw"]))
            if entry.get("executable"):
                executable_files.append(path)
        elif kind == "directory":
            path.mkdir(parents=True, exist_ok=True)
        elif kind == "link":
            target = inside(directory, str(path.parent.relative_to(directory) / entry["target"]))
            links.append((path, target))
    run_downloads(jobs, reporter, cancel)
    for path in executable_files:
        path.chmod(path.stat().st_mode | 0o111)
    for path, target in links:
        check_cancel(cancel)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(target, target_is_directory=target.is_dir())
    check_cancel(cancel)
    (directory / ".complete").write_text("ok\n", encoding="ascii")
    return find_java(root, required)


def install_version(version, settings=None, root=GAME_DIR, app_dir=APP_DIR,
                    catalog=None, progress=None, cancel=None):
    """Instala/repara somente a versão selecionada e suas dependências."""
    settings = settings or {}
    reporter = Reporter(progress)
    check_cancel(cancel)
    reporter.report(f"Conferindo Minecraft {version}…", True)
    ensure_manifest(root, version, catalog, app_dir, reporter, cancel)
    manifest = resolve_version(root, version)
    ensure_java(manifest, root, settings, reporter, cancel)
    client_id = manifest["_client"]
    client_manifest = read_json(version_file(root, client_id))
    client_path = inside(root / "versions", f"{client_id}/{client_id}.jar")
    client_spec = client_manifest.get("downloads", {}).get("client")
    jobs = []
    if client_spec:
        jobs.append((client_path, {**client_spec, "label": f"Minecraft {client_id}"}))
    elif not client_path.is_file():
        raise LaunchError(f"O perfil {version} precisa do JAR instalado pelo seu instalador original.")
    artifacts, natives = library_entries(root, manifest)
    jobs.extend(artifacts)
    jobs.extend((path, spec) for path, spec, _ in natives)
    logging = manifest.get("logging", {}).get("client", {}).get("file")
    if logging:
        jobs.append((inside(root / "assets" / "log_configs", logging["id"]), logging))
    asset = manifest.get("assetIndex", {})
    asset_id = asset.get("id", manifest.get("assets", "legacy"))
    asset_path = inside(root / "assets" / "indexes", f"{asset_id}.json")
    reporter.report("Conferindo o índice de assets…", True)
    if asset.get("url"):
        download_file(asset_path, asset, cancel)
    elif not asset_path.is_file():
        raise LaunchError("Este perfil não informa como baixar o índice de assets.")
    for name, item in read_json(asset_path).get("objects", {}).items():
        digest = item.get("hash", "")
        if len(digest) != 40 or any(c not in "0123456789abcdef" for c in digest):
            raise LaunchError("Hash inválido no índice de assets.")
        relative = f"{digest[:2]}/{digest}"
        jobs.append((inside(root / "assets" / "objects", relative),
                     {"url": ASSET_URL + relative, "sha1": digest, "size": item.get("size"),
                      "label": name.rsplit("/", 1)[-1]}))
    run_downloads(jobs, reporter, cancel)
    check_cancel(cancel)
    reporter.report(f"Minecraft {version} pronto. Iniciando…", True)
