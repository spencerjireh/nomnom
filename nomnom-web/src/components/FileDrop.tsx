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

/** The composer's staging area: a multi-file dropzone with a free-text box
 * below it — no mode to pick, whatever is filled goes out on send. Each file
 * and the text (as message.txt) becomes its own payload. Oversized items are
 * rejected here, before any crypto. */
export function FileDrop({
  onChange,
  disabled,
}: {
  onChange: (payloads: StagedPayload[]) => void;
  disabled?: boolean;
}) {
  const [files, setFiles] = useState<File[]>([]);
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  function emit(nextFiles: File[], nextText: string) {
    const payloads: StagedPayload[] = nextFiles.map((f) => ({
      name: f.name,
      size: f.size,
      read: () => f.arrayBuffer(),
    }));
    if (nextText) {
      const bytes = enc.encode(nextText);
      payloads.push({
        name: "message.txt",
        size: bytes.length,
        read: async () => bytes.slice().buffer,
      });
    }
    onChange(payloads);
  }

  function addFiles(incoming: File[]) {
    if (incoming.length === 0) return;
    const tooBig = incoming.filter((f) => f.size > MAX_PAYLOAD_BYTES);
    const ok = incoming.filter((f) => f.size <= MAX_PAYLOAD_BYTES);
    setError(
      tooBig.length
        ? `too big (limit is ${fmtSize(MAX_PAYLOAD_BYTES)}): ${tooBig
            .map((f) => f.name)
            .join(", ")}`
        : null,
    );
    if (ok.length === 0) return;
    const next = [...files, ...ok];
    setFiles(next);
    emit(next, text);
  }

  function removeFile(i: number) {
    const next = files.filter((_, j) => j !== i);
    setFiles(next);
    emit(next, text);
  }

  function takeText(t: string) {
    setText(t);
    if (enc.encode(t).length > MAX_PAYLOAD_BYTES) {
      setError(`message too big. limit is ${fmtSize(MAX_PAYLOAD_BYTES)}.`);
      emit(files, "");
      return;
    }
    setError(null);
    emit(files, t);
  }

  return (
    <div className="filedrop">
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
          if (!disabled) addFiles(Array.from(e.dataTransfer.files));
        }}
      >
        <input
          ref={inputRef}
          type="file"
          multiple
          hidden
          disabled={disabled}
          onChange={(e) => {
            addFiles(Array.from(e.target.files ?? []));
            e.target.value = ""; // re-picking the same file must fire again
          }}
        />
        <span className="dim">
          {files.length === 0
            ? "drop files here, or click to choose"
            : "drop more files, or click to add"}
        </span>
      </div>
      {files.length > 0 && (
        <ul className="staged-list" role="list">
          {files.map((f, i) => (
            <li key={`${f.name}-${i}`} className="staged-file">
              <span className="row-name">{f.name}</span>
              <span className="dim small">· {fmtSize(f.size)}</span>
              <button
                type="button"
                className="chip danger staged-remove"
                aria-label={`remove ${f.name}`}
                disabled={disabled}
                onClick={() => removeFile(i)}
              >
                ✕
              </button>
            </li>
          ))}
        </ul>
      )}
      <textarea
        className="textbox"
        placeholder="and/or paste a message… (sends as message.txt)"
        value={text}
        disabled={disabled}
        onChange={(e) => takeText(e.target.value)}
        rows={3}
      />
      {error && <p className="err">{error}</p>}
    </div>
  );
}
