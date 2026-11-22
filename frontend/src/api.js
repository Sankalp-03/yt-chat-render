async function request(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
  });

  let data = {};
  try { data = await response.json(); } catch { /* OAuth redirects are navigations, not API calls. */ }
  if (!response.ok) {
    throw new Error(data.error || `Request failed (${response.status})`);
  }
  return data;
}

export const api = {
  status: (platform) => request(`/status?platform=${encodeURIComponent(platform)}`),
  config: (payload) => request("/config", {
    method: "POST", body: JSON.stringify(payload),
  }),
  start: (payload) => request("/start", {
    method: "POST", body: JSON.stringify(payload),
  }),
  stop: (platform) => request("/stop", {
    method: "POST", body: JSON.stringify({ platform }),
  }),
};