import { useRef, useState } from "react";
import { MAX_PAYLOAD_BYTES } from "../config";
import { fmtSize } from "../util/format";

/** A staged payload: metadata now, bytes read lazily at send time (the worker
 * transfer detaches the buffer, so we read a fresh one per send). */
export interface StagedPayload {
  name: string;
  size: number;
  read: () => Promise<ArrayBuffer>;
}

const enc = new TextEncoder();


/** File picker + drag-drop + text mode. Rejects >100 MB before any crypto. */
export function FileDrop({
  onChange,
  disabled,
}: {
  onChange: (p: StagedPayload | null) => void;
  disabled?: boolean;
}) {
  const [mode, setMode] = useState<"file" | "text">("file");
  const [fileName, setFileName] = useState<string | null>(null);
  const [fileSize, setFileSize] = useState(0);
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  function takeFile(f: File | null) {
    setError(null);
    if (!f) {
      setFileName(null);
      onChange(null);
      return;
    }
    if (f.size > MAX_PAYLOAD_BYTES) {
      setFileName(null);
      setError(`too big (${fmtSize(f.size)}). limit is ${fmtSize(MAX_PAYLOAD_BYTES)}.`);
      onChange(null);
      return;
    }
    setFileName(f.name);
    setFileSize(f.size);
    onChange({ name: f.name, size: f.size, read: () => f.arrayBuffer() });
  }

  function takeText(t: string) {
    setText(t);
    setError(null);
    if (!t) {
      onChange(null);
      return;
    }
    const bytes = enc.encode(t);
    if (bytes.length > MAX_PAYLOAD_BYTES) {
      setError(`too big (${fmtSize(bytes.length)}). limit is ${fmtSize(MAX_PAYLOAD_BYTES)}.`);
      onChange(null);
      return;
    }
    onChange({
      name: "message.txt",
      size: bytes.length,
      read: async () => bytes.slice().buffer,
    });
  }

  function switchMode(next: "file" | "text") {
    setMode(next);
    setError(null);
    if (next === "file") {
      setText("");
    } else {
      setFileName(null);
      if (inputRef.current) inputRef.current.value = "";
    }
    onChange(null);
  }

  return (
    <div className="filedrop">
      <div className="filedrop-tabs">
        <button
          type="button"
          className={mode === "file" ? "chip active" : "chip"}
          onClick={() => switchMode("file")}
          disabled={disabled}
        >
          file
        </button>
        <button
          type="button"
          className={mode === "text" ? "chip active" : "chip"}
          onClick={() => switchMode("text")}
          disabled={disabled}
        >
          text
        </button>
      </div>

      {mode === "file" ? (
        <div
          className={dragging ? "dropzone over" : "dropzone"}
          onClick={() => !disabled && inputRef.current?.click()}
          onDragOver={(e) => {
            e.preventDefault();
            if (!disabled) setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragging(false);
            if (!disabled) takeFile(e.dataTransfer.files[0] ?? null);
          }}
        >
          <input
            ref={inputRef}
            type="file"
            hidden
            disabled={disabled}
            onChange={(e) => takeFile(e.target.files?.[0] ?? null)}
          />
          {fileName ? (
            <span>
              {fileName} <span className="dim">· {fmtSize(fileSize)}</span>
            </span>
          ) : (
            <span className="dim">drop a file here, or click to choose</span>
          )}
        </div>
      ) : (
        <textarea
          className="textbox"
          placeholder="paste or type a message…"
          value={text}
          disabled={disabled}
          onChange={(e) => takeText(e.target.value)}
          rows={5}
        />
      )}
      {error && <p className="err">{error}</p>}
    </div>
  );
}
