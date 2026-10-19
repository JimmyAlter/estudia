import { vi } from "vitest";

type Route = (body: unknown) => unknown;

/**
 * Reemplaza fetch por respuestas fijas según "MÉTODO /ruta".
 * Una ruta puede ser un valor, una función (que puede devolver un `Response`
 * para otro código de estado, o tirar un error para simular el servidor caído).
 */
export function mockApi(routes: Record<string, Route | unknown>) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const method = (init?.method ?? "GET").toUpperCase();
    const key = `${method} ${String(input)}`;
    if (!(key in routes)) {
      return new Response(JSON.stringify({ detail: "no encontrada" }), { status: 404 });
    }
    const route = routes[key];
    // Un FormData (subida de un archivo) llega tal cual; lo demás es JSON.
    const body = init?.body instanceof FormData ? init.body : init?.body ? JSON.parse(String(init.body)) : undefined;
    const data = typeof route === "function" ? (route as Route)(body) : route;
    if (data instanceof Response) return data;
    return new Response(JSON.stringify(data), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

export function jsonResponse(status: number, data: unknown): Response {
  return new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
}
