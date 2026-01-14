import os
import re
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs, unquote
import fitz
from models.models import CaseContext

def parse_pdf_for_attachments(downloaded_pdf_path, att_name_desc):
    doc = fitz.open(downloaded_pdf_path)
    att_links = []

    filename_count = {}  

    for page_num in range(len(doc)):
        page = doc[page_num]
        links = page.get_links()
        for link in links:
            uri = link.get('uri', '')
            if "https://esft.intel.com/sftservices/download" in uri:
                parsed_url = urlparse(uri)
                query_params = parse_qs(parsed_url.query)
                filename = unquote(query_params.get('FileName', [''])[0])

                if filename in filename_count:
                    filename_count[filename] += 1
                    base, ext = os.path.splitext(filename)
                    numbered_filename = f"{base}_{filename_count[filename]}{ext}"
                else:
                    filename_count[filename] = 1
                    numbered_filename = filename
                
                desc = att_name_desc.get(filename)      
                print("📦 uri filename:", numbered_filename)
                if desc is None:
                    print(f"⚠️ File name in the PDF has no matching description: '{filename}'")
                    desc = [None, "(No description)"]

                att_links.append([numbered_filename, uri, desc])

    return att_links

def parse_pdf_for_all_info(ips_pdf_path, case_context: CaseContext):
    doc = fitz.open(ips_pdf_path)
    print("-----doc-----", doc)
    print("-----len(doc)-----", len(doc))
    
    all_blocks = []
    for page in doc:
        blocks = page.get_text("blocks")
        blocks.sort(key=lambda b: (b[1], b[0]))  # sort top-to-bottom, left-to-right
        for b in blocks:
            text = b[4].strip()
            if text:
                all_blocks.append({
                    "page": page.number + 1,
                    "bbox": b[:4],
                    "text": text
                })

    recent_comments = []

    # ---- 1. Subject ----
    for block in all_blocks:
        if "subject" in block["text"].lower():
            case_context.subject = block["text"].replace("Subject", "")
            break

    # ---- 2. Case Description ----
    case_context.description = ""
    start_collecting = False
    sub_cat = ""
    for block in all_blocks:
        text = block["text"]
        if "Case Subcategory" in text:
            t = text.strip().splitlines()
            for idx, line in enumerate(t):
                if line.strip() == "Case Subcategory":
                    sub_cat = t[idx+1]
                    break
        if not start_collecting and "Subject" in text:
            start_collecting = True
            continue  

        if start_collecting:
            if "Coveo Search:" in text:
                break
            if "Case\nDescription" in text:
                continue
            case_context.description += text

    # ---- 3. Environment Details ----
    case_context.env_detail = extract_env(all_blocks)

    # ---- 4. Recent Comments ----
    in_comment_section = False
    att_desc_dict = {}
    for block in all_blocks:
        if "Recent Comments" in block["text"]:
            in_comment_section = True
            continue
        if in_comment_section:
            lines = block["text"].splitlines()
            if lines:
                recent_comments.append(block["text"])
                if "Download link" in block["text"]:
                    att_desc = lines[-1].split(" ")[-1] 
                    for line in lines:
                        match = re.search(r'\b[\w\-]+?\.[\w\-]+\b', line)
                        if match:
                            att_desc_dict[match.group()] = att_desc

    case_context.backend_id = ""
    case_context.subcategory = sub_cat
    case_context.comments = "\n\n".join(recent_comments).strip()
    case_context.attachment_info = att_desc_dict

    return case_context


def parse_html_table(html_string):
    soup = BeautifulSoup(html_string, "html.parser")
    table = soup.find("table")
    
    result = {}
    if not table:
        return result
    
    rows = table.find_all("tr")
    for row in rows[1:]:  # skip header
        cols = row.find_all("td")
        if len(cols) == 2:
            key = cols[0].get_text(strip=True)
            value = cols[1].get_text(strip=True)
            result[key] = value
    
    return result


def pair_v_with_q(v_list, q_list):
    result = {}
    for v in v_list:
        v_text = v["text"]
        v_y = v["bbox"][1]

        closest_q = None
        min_distance = float("inf")

        for q in q_list:
            q_y = q["bbox"][1]
            distance = abs(q_y - v_y)

            if distance < min_distance:
                min_distance = distance
                closest_q = q

        if closest_q:
            q_text = closest_q["text"]
            result[q_text] = v_text
            q_list.remove(closest_q)
            
        else:
            print(f"⚠️ cant find q for v: {v_text}")

    for q in q_list:
        result[q["text"]] = ""

    return result

def extract_env(all_blocks):
    env_blocks = []
    env_info_start = False
    env_dict_not_matched = {"q":[], "v":[]}
    env_matched = {}
    bbox_left = set()
    for block in all_blocks:
        text = block["text"]
        if "Question" in text and "Response" in text:
            env_info_start = True
            continue  
        if "Case Service Level:" in text:
            break
        if env_info_start:
            bbox_left.add(block['bbox'][0])
            env_blocks.append(block)
    bbox_left = list(sorted(bbox_left))

    for block in env_blocks:
        if block['bbox'][0] == bbox_left[0]:
            continue
        elif block['bbox'][0] == bbox_left[1]:
            if (len(bbox_left) < 3) or (len(bbox_left) == 3 and block['bbox'][2] > bbox_left[2]):
                t = block['text'].split("\n")
                env_matched[t[0]] = t[1] if len(t) > 1 else ""
            else:
                env_dict_not_matched["q"].append(block)
        else: 
            env_dict_not_matched["v"].append(block)

    paired_result = pair_v_with_q(env_dict_not_matched["v"], env_dict_not_matched["q"])

    env_dict = {**paired_result, **env_matched}
    return env_dict

