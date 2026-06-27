# %% libraries
import json
import os
import time
from pathlib import Path

import pandas as pd
from google import genai
from google.genai import types
from werkzeug.utils import secure_filename


MODEL_NAME = "gemini-2.5-flash"
BATCH_SIZE = 50

def srt_to_dataframe(file_path):
    """
    Convert SRT subtitle file into pandas DataFrame.

    Output columns:
    index, start_time, end_time, dialogue

    Multi-line subtitles are preserved using <n>
    """

    with open(file_path, "r", encoding="utf-8-sig") as file:
        content = file.read()

    blocks = content.strip().split("\n\n")
    rows = []

    for block in blocks:
        lines = block.strip().split("\n")

        if len(lines) < 3:
            continue

        subtitle_index = lines[0].strip()
        time_line = lines[1].strip()
        start_time, end_time = time_line.split(" --> ")
        dialogue = "<n>".join(lines[2:]).strip()

        rows.append(
            {
                "index": int(subtitle_index),
                "start_time": start_time,
                "end_time": end_time,
                "dialogue": dialogue,
            }
        )

    return pd.DataFrame(rows)


def trim_df(df):
    return df[["index", "dialogue"]].copy()


def split_dataframe_into_batches(df, batch_size=BATCH_SIZE):
    batches = []

    for start_idx in range(0, len(df), batch_size):
        batch_df = df.iloc[start_idx:start_idx + batch_size]
        batches.append(batch_df)

    return batches


def dataframe_batch_to_json(batch_df):
    records = batch_df.to_dict(orient="records")
    return json.dumps(records, ensure_ascii=False, indent=2)


def build_translation_prompt(input_lang, target_lang):
    """
    Build the instruction prompt for Gemini.
    """

    prompt = f"""
        You are given a JSON array containing subtitle dialogue from an {input_lang} tv show script which explains the dialogue type.

        Each object contains:
        - "index"
        - "dialogue"

        {target_lang.upper()} SUBTITLES

        I am uploading a subtitle file in {input_lang}.
        You need to translate it into {target_lang}.
        Please keep the translation natural, fluent, and concise.
        Don't expand the translation by putting in extraneous facts.
        Don't translate in fragments.
        If a sentence is across more than one subtitle, translate the full sentence to keep proper sentence structure, and then divide it across the original timestamps.
        I want the same number of subtitles as in the source.
        Do not change the timestamps.
        Do not change single quotes to double quotes.

        Don't keep any reference link in your output.
        Give the output in JSON format.

        Technical instructions for this task:
        1. Preserve the exact JSON structure.
        2. Preserve the exact "index" value.
        3. Return ONE output object for every input object.
        4. Do not reorder rows.
        5. Do not omit any rows.
        6. Do not add explanations or commentary.
        7. Do not wrap the output in markdown.
        8. Output STRICT VALID JSON ONLY.
        9. Do not output raw SRT blocks. 

        For each object:
        - Keep the original "dialogue" unchanged.
        - Add a new field called "translated_dialogue".

        Translation rules:
        - Translate from {input_lang} into natural, fluent, concise {target_lang}.
        - Preserve any "<n>" markers exactly as they appear.
        - Preserve single quotes exactly. Do not convert them to double quotes.
        - Keep the same subtitle count as the source.
        - If a sentence spans multiple subtitle rows, use the surrounding rows for context so the translation reads naturally, but still return one translated subtitle row per input row.
        - Do not add reference links.
        - Do not add extra facts or explanations.

        Return only a JSON array.
    """
    return prompt.strip()


def call_gemini_for_batch(
    client,
    batch_json,
    input_lang,
    target_lang,
    model_name=MODEL_NAME,
):
    prompt = build_translation_prompt(
        input_lang=input_lang,
        target_lang=target_lang,
    )

    full_prompt = f"""
        {prompt}

        Here is the input JSON array:
        {batch_json}
    """

    response = client.models.generate_content(
        model=model_name,
        contents=full_prompt,
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
        ),
    )

    response_text = response.text.strip()

    #adding this to debug gemini forgetting "translated_dialogue" field
    print("\n--- RAW GEMINI RESPONSE ---")
    print(response_text[:2000])  # print first 2000 chars to avoid flooding terminal
    print("--- END RAW RESPONSE ---\n")

    return json.loads(response_text)


def normalize_batch_result(batch_result, batch_num):
    if isinstance(batch_result, dict):
        batch_result = [batch_result]

    if not isinstance(batch_result, list):
        raise ValueError(
            f"Batch {batch_num} returned {type(batch_result).__name__}, expected a JSON array."
        )

    normalized_rows = []

    for row_num, row in enumerate(batch_result, start=1):
        if not isinstance(row, dict):
            raise ValueError(
                f"Batch {batch_num}, row {row_num} returned {type(row).__name__}, expected an object."
            )

        if "index" not in row or "translated_dialogue" not in row:
            missing = [k for k in ["index", "dialogue", "translated_dialogue"] if k not in row]
            raise ValueError(
                f"Batch {batch_num}, row {row_num} is missing: {missing}. "
                f"Found keys: {sorted(row.keys())}. "
                f"Full row content: {row}"   # ← this shows us exactly what Gemini returned
            )

        normalized_rows.append(row)

    return normalized_rows


def emit_progress(progress_callback, current_batch, total_batches, message):
    if progress_callback is None:
        return

    progress_callback(
        {
            "current_batch": current_batch,
            "total_batches": total_batches,
            "percent": int((current_batch / total_batches) * 100) if total_batches else 100,
            "message": message,
        }
    )


def get_output_file_name(file_path, requested_name=None):
    input_path = Path(file_path)

    if requested_name:
        requested_stem = Path(requested_name).stem
        safe_stem = secure_filename(requested_stem)
        if safe_stem:
            return f"{safe_stem}.srt"

    return f"{input_path.stem}_translated.srt"


def main(
    file_path,
    input_lang,
    target_lang,
    batch_size=BATCH_SIZE,
    progress_callback=None):

    df = srt_to_dataframe(file_path)
    trimmed_df = trim_df(df)

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY environment variable is not set.")

    client = genai.Client(api_key=api_key)

    batch_list = split_dataframe_into_batches(trimmed_df, batch_size=batch_size)
    total_batches = len(batch_list)
    all_results = []

    for batch_num, batch_df in enumerate(batch_list):
        batch_message = f"Processing batch {batch_num + 1} of {total_batches}"
        print(batch_message)
        emit_progress(progress_callback, batch_num, total_batches, batch_message)

        batch_json = dataframe_batch_to_json(batch_df)

        MAX_RETRIES = 3
        normalized_batch_result = None
        last_error = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                batch_result = call_gemini_for_batch(
                    client=client,
                    batch_json=batch_json,
                    input_lang=input_lang,
                    target_lang=target_lang,
                )

                normalized_batch_result = normalize_batch_result(
                    batch_result=batch_result,
                    batch_num=batch_num + 1,
                )

                response_message = f"Gemini batch {batch_num + 1} succeeded on attempt {attempt} of {MAX_RETRIES}."
                print(response_message)
                break

            except ValueError as e:
                last_error = e
                print(f"WARNING: Batch {batch_num + 1}, attempt {attempt} of {MAX_RETRIES} failed: {e}")

                if attempt < MAX_RETRIES:
                    print(f"Retrying in 2 seconds...")
                    time.sleep(2)
                else:
                    raise ValueError(
                        f"Batch {batch_num + 1} of {total_batches} failed after "
                        f"{MAX_RETRIES} attempts. Last error: {last_error}"
                    )

        all_results.extend(normalized_batch_result)
        emit_progress(progress_callback, batch_num + 1, total_batches, response_message)

        time.sleep(1)

    gemini_output_df = pd.DataFrame(all_results)
    final_df = df.merge(
        gemini_output_df[["index", "translated_dialogue"]],
        on="index",
        how="left",
    )

    emit_progress(progress_callback, total_batches, total_batches, "Merging batch results.")

    return gemini_output_df, final_df


def process_srt_file(
    file_path,
    input_lang,
    target_lang,
    batch_size=BATCH_SIZE,
    output_file_name=None,
    output_dir="output",
    progress_callback=None,
):
    """
    Run subtitle translation for an SRT file and write the processed SRT to disk.
    """

    _, final_df = main(
        file_path=file_path,
        input_lang=input_lang,
        target_lang=target_lang,
        batch_size=batch_size,
        progress_callback=progress_callback,
    )

    output_dir_path = Path(output_dir)
    output_dir_path.mkdir(parents=True, exist_ok=True)

    output_filename = get_output_file_name(
        file_path=file_path,
        requested_name=output_file_name,
    )

    output_path = output_dir_path / output_filename
    dataframe_to_srt(final_df, output_path)
    emit_progress(progress_callback, 1, 1, f"Output ready: {output_filename}")

    return output_path


def dataframe_to_srt(final_df, output_path):
    srt_blocks = []

    for _, row in final_df.iterrows():
        subtitle_index = int(row["index"])
        start_time = row["start_time"]
        end_time = row["end_time"]

        if pd.notna(row["translated_dialogue"]):
            subtitle_text = row["translated_dialogue"]
        else:
            subtitle_text = row["dialogue"]

        subtitle_text = subtitle_text.replace("<n>", "\n")

        block = f"{subtitle_index}\n{start_time} --> {end_time}\n{subtitle_text}"
        srt_blocks.append(block)

    srt_content = "\n\n".join(srt_blocks)

    with open(output_path, "w", encoding="utf-8") as file:
        file.write(srt_content)

    print(f"SRT file saved to: {output_path}")
