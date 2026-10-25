import { afterEach, describe, expect, it } from "vitest";
import { setToken } from "./api";
import { chatSessionId } from "./chatSession";

afterEach(() => {
  sessionStorage.clear();
});

describe("chat session and sign-in", () => {
  it("gives each sign-in a new chat session and drops it on sign-out", () => {
    setToken("first-token");
    const first = chatSessionId();
    setToken("second-token");
    expect(chatSessionId()).not.toBe(first);
    setToken(null);
    expect(sessionStorage.getItem("reconcilo.chatSession")).toBeNull();
    expect(sessionStorage.getItem("ghostledger.token")).toBeNull();
  });
});
