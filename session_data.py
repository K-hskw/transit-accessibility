"""訪問者ごとのデータ（アップロードしたGTFS・人口・施設）を扱う

公開環境では1つのコンテナを全訪問者で共有する。以前はアップロードを
共有ディレクトリ（gtfs_data_custom/ や bus_edges_custom.csv）に書いていたため、
誰かがZIPを上げると、その後に開いた全員に別の都市が表示されていた。
ここでは訪問者ごとの一時ディレクトリにだけ書き、共有ファイルには触れない。
"""
import os
import shutil
import tempfile
import zipfile

from build_network import build_network
from transit_engine import TransitEngine

# 展開後の合計サイズの上限。ZIP爆弾で共有コンテナのディスクとメモリを
# 使い切られないようにする。道内の中規模都市のGTFSは展開後数十MB程度。
MAX_UNZIPPED_BYTES = 500 * 1024 * 1024

REQUIRED_GTFS_FILES = ("stops.txt", "stop_times.txt", "trips.txt", "routes.txt", "calendar.txt")


def new_work_dir():
    """訪問者1人ぶんの作業ディレクトリを作る"""
    return tempfile.mkdtemp(prefix="kuhaku_")


def remove_work_dir(path):
    if path and os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)


def safe_filename(name, default="upload.csv"):
    """アップロード名からディレクトリ部分を落とす（../ などで外に書かせない）"""
    base = os.path.basename(str(name or "")).strip()
    return base if base and base not in (".", "..") else default


def extract_gtfs(fileobj, work_dir):
    """GTFSのZIPを work_dir/gtfs に展開し、stops.txt のあるフォルダを返す"""
    dest = os.path.join(work_dir, "gtfs")
    if os.path.exists(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)
    with zipfile.ZipFile(fileobj) as z:
        total = sum(i.file_size for i in z.infolist())
        if total > MAX_UNZIPPED_BYTES:
            raise ValueError(
                f"展開後のサイズが大きすぎます（{total / 1024 / 1024:,.0f}MB、"
                f"上限{MAX_UNZIPPED_BYTES // 1024 // 1024}MB）")
        # extractall は絶対パスや .. を含む名前を展開先の外に書かない（Python 3.12）
        z.extractall(dest)

    for root, _dirs, files in os.walk(dest):
        if "stops.txt" in files and "stop_times.txt" in files:
            missing = [f for f in REQUIRED_GTFS_FILES if f not in files]
            if missing:
                raise ValueError(f"GTFSに必要なファイルがありません: {', '.join(missing)}")
            return root
    raise ValueError("ZIPの中に stops.txt と stop_times.txt が見つかりません")


def build_uploaded_engine(fileobj, work_dir):
    """アップロードされたGTFSから、この訪問者専用のエンジンを作る

    エッジは work_dir に書く。build_network の既定の出力先（カレント
    ディレクトリ）に書くと、共有の既定エッジ（室蘭）を上書きしてしまう。
    """
    gtfs_path = extract_gtfs(fileobj, work_dir)
    build_network(gtfs_path, work_dir)
    return TransitEngine(gtfs_dir=gtfs_path, edges_dir=work_dir)


def save_upload(fileobj, work_dir, name):
    """CSVなどのアップロードを作業ディレクトリに保存してパスを返す

    元のファイル名を残すのは、人口CSVの名前（例: ..._01204旭川市.csv）から
    政策文書に載せる自治体名を取り出すため。
    """
    path = os.path.join(work_dir, safe_filename(name))
    with open(path, "wb") as f:
        f.write(fileobj.getvalue() if hasattr(fileobj, "getvalue") else fileobj.read())
    return path
