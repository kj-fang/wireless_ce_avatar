import zipfile
import os
import rarfile
import py7zr
import shutil
import tempfile
import traceback

def find_compressed_files(directory):
    """Find all compressed files (.zip, .rar, .7z) in directory recursively."""
    compressed_files = []
    for dirpath, _, filenames in os.walk(directory):
        for filename in filenames:
            if filename.lower().endswith(('.zip', '.rar', '.7z')):
                compressed_files.append(os.path.join(dirpath, filename))
    return compressed_files


def filter_files(type, etl_files):
    filtered_tiles = []

    if type == "wifi":
        for file in etl_files:
            if 'wifi' in os.path.basename(file).lower() and 'history' not in file.lower():
                filtered_tiles.append(file)

    elif type == "bt":
        for file in etl_files:
            if os.path.basename(file).lower().startswith(('ibtusb-', 'ibtpci-')):
                filtered_tiles.append(file)

    elif type == "fw":
        for file in etl_files:
            if os.path.basename(file).lower().startswith('wrt-fw'):
                filtered_tiles.append(file)
    
    return filtered_tiles


def list_etl_files(root_folder):
    etl_files = []
    for dirpath, _, filenames in os.walk(root_folder):
        for filename in filenames:
            if '.etl' in filename.lower():
                etl_files.append(os.path.join(dirpath, filename))
    return etl_files
        

def extract_archive(archive, extract_to):
    """Extract archive contents to target directory."""
    print(f"Extracting to {extract_to} ({len(archive.infolist())} items)")
    
    with tempfile.TemporaryDirectory() as temp_dir:
        for member in archive.infolist():
            if member.is_dir():
                continue
                
            filename = member.filename.strip().replace('/', os.sep)
            
            try:
                archive.extract(member, path=temp_dir)
                src_path = os.path.join(temp_dir, filename)
                dst_path = os.path.normpath(os.path.join(extract_to, filename))
                
                if not os.path.isfile(src_path):
                    continue
                    
                dst_dir = os.path.dirname(dst_path)
                os.makedirs(dst_dir, exist_ok=True)
                
                # Fix naming issue
                if "AutoLoggParser" in dst_path:
                    dst_path = dst_path.replace("AutoLoggParser", "AutoLogParser")
                    
                shutil.move(src_path, dst_path)
                
            except Exception as e:
                print(f"Error extracting {filename}: {e}")
                continue
    
    return extract_to

def unzip_file(file_path, extract_to, already_downloaded):
    """Extract compressed file to destination."""
    if already_downloaded:
        return extract_to
        
    try:
        if file_path.endswith('.zip'):
            with zipfile.ZipFile(file_path, 'r') as archive:
                return extract_archive(archive, extract_to)
        elif file_path.endswith('.rar'):
            with rarfile.RarFile(file_path, 'r') as archive:
                return extract_archive(archive, extract_to)
        elif file_path.endswith('.7z'):
            with py7zr.SevenZipFile(file_path, mode='r') as archive:
                return extract_archive(archive, extract_to)
    except Exception as e:
        print(f"Extraction failed for {file_path}: {e}")
        
    return extract_to

def process_single_zip(zip_path, download_path_tmp, already_downloaded):
    """Process ZIP file and categorize extracted files."""
    folder_name = os.path.splitext(os.path.basename(zip_path))[0].replace(" ", "_")
    download_path = os.path.join(download_path_tmp, folder_name)
    os.makedirs(download_path, exist_ok=True)
    
    # Initialize result lists
    wifi_files, ddd_files, bt_files, fw_files = [], [], [], []
    processed_files = set()
    unzip_pending = [os.path.abspath(zip_path)]
    
    while unzip_pending:
        file_to_unzip = unzip_pending.pop(0)
        
        if file_to_unzip in processed_files:
            continue
            
        try:
            extract_to = unzip_file(file_to_unzip, download_path, already_downloaded)
        except Exception:
            continue
            
        # Process ETL files
        etl_files = list_etl_files(extract_to)

        wifi_files.extend(filter_files('wifi', etl_files))
        bt_files.extend(filter_files('bt', etl_files))
        fw_files.extend(filter_files('fw', etl_files))
        
        # Find DDD files (non-compressed files containing 'ddd')
        compressed_exts = ('.zip', '.rar', '.7z', '.tar', '.gz', '.xz')
        for root, _, files in os.walk(extract_to):
            for fname in files:
                if 'ddd' in fname.lower() and not fname.lower().endswith(compressed_exts):
                    ddd_files.append(os.path.abspath(os.path.join(root, fname)))
        
        processed_files.add(file_to_unzip)
        
        # Find nested compressed files
        new_compressed = find_compressed_files(extract_to)
        for new_file in new_compressed:
            new_file = os.path.abspath(new_file)
            if "history" not in new_file.lower() and new_file not in processed_files:
                unzip_pending.append(new_file)
    
    # Remove duplicates
    wifi_files = list(dict.fromkeys(wifi_files))
    ddd_files = list(dict.fromkeys(ddd_files))
    bt_files = list(dict.fromkeys(bt_files))
    fw_files = list(dict.fromkeys(fw_files))
    
    print(f"WiFi files: {len(wifi_files)}")
    print(f"DDD files: {len(ddd_files)}")
    print(f"BT files: {len(bt_files)}")
    print(f"FW files: {len(fw_files)}")
    
    return wifi_files, ddd_files, bt_files, fw_files