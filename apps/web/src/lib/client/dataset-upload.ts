import type { ApiDatasetUploadInitResponse } from "@/lib/api-types";

export interface UploadProgress {
  loaded: number;
  total: number;
  lengthComputable: boolean;
}

export async function uploadDatasetObject(
  target: ApiDatasetUploadInitResponse,
  file: File,
  options: {
    onProgress: (progress: UploadProgress) => void;
    signal: AbortSignal;
  },
): Promise<void> {
  if (target.status !== "pending" || target.method !== "PUT" || !target.upload_url) {
    throw new Error("Manager did not return a writable Dataset target");
  }
  const uploadUrl = new URL(target.upload_url);
  if (!['http:', 'https:'].includes(uploadUrl.protocol) || uploadUrl.hash ||
    uploadUrl.username || uploadUrl.password) {
    throw new Error("Manager returned an invalid Dataset target");
  }
  const headers = browserUploadHeaders(target.upload_headers);
  options.onProgress({ loaded: 0, total: file.size, lengthComputable: false });
  if (uploadUrl.origin === window.location.origin) {
    const response = await fetch(uploadUrl, {
      body: file,
      credentials: "omit",
      headers,
      method: "PUT",
      redirect: "error",
      referrerPolicy: "no-referrer",
      signal: options.signal,
    });
    if (!response.ok) throw new Error(`Dataset object upload failed (HTTP ${response.status})`);
    options.onProgress({ loaded: file.size, total: file.size, lengthComputable: false });
    return;
  }
  await uploadWithProgress(uploadUrl, headers, file, options);
}

export function browserUploadHeaders(value: Record<string, string>): Headers {
  const headers = new Headers();
  for (const [rawName, rawValue] of Object.entries(value)) {
    const name = rawName.toLowerCase();
    if (name === "content-length") continue;
    if (![
      "content-type",
      "x-amz-checksum-sha256",
      "x-amz-meta-sha256",
      "x-rvc-upload-token",
    ].includes(name)) {
      throw new Error("Manager returned a disallowed Dataset upload header");
    }
    headers.set(name, rawValue);
  }
  return headers;
}

function uploadWithProgress(
  uploadUrl: URL,
  headers: Headers,
  file: File,
  options: {
    onProgress: (progress: UploadProgress) => void;
    signal: AbortSignal;
  },
): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const abort = () => xhr.abort();
    options.signal.addEventListener("abort", abort, { once: true });
    const cleanup = () => options.signal.removeEventListener("abort", abort);
    xhr.open("PUT", uploadUrl, true);
    xhr.withCredentials = false;
    for (const [name, value] of headers) xhr.setRequestHeader(name, value);
    xhr.upload.onprogress = (event) => {
      options.onProgress({
        loaded: event.loaded,
        total: event.lengthComputable ? event.total : file.size,
        lengthComputable: event.lengthComputable,
      });
    };
    xhr.onload = () => {
      cleanup();
      let finalOrigin: string | null = null;
      try {
        finalOrigin = new URL(xhr.responseURL || uploadUrl).origin;
      } catch {
        // Invalid final URLs are rejected below without disclosing them.
      }
      if (finalOrigin !== uploadUrl.origin) {
        reject(new Error("Dataset upload target redirected outside its approved origin"));
      } else if (xhr.status >= 200 && xhr.status < 300) {
        options.onProgress({ loaded: file.size, total: file.size, lengthComputable: true });
        resolve();
      } else {
        reject(new Error(`Dataset object upload failed (HTTP ${xhr.status})`));
      }
    };
    xhr.onerror = () => {
      cleanup();
      reject(new Error("Dataset object upload could not reach the approved target"));
    };
    xhr.onabort = () => {
      cleanup();
      reject(new DOMException("Dataset upload was cancelled", "AbortError"));
    };
    xhr.send(file);
  });
}
