// Triple-Diffie-Hellman session key. Mirrors nomnom.py `_session_key` and the
// initiator/responder wrappers. All key params are hex (format(int,"x")).

import { sha256 } from "./primitives";
import { dhSharedBytes, dhPubBytes } from "./dh";
import { SESSION_TAG, SESSION_BIND_PREFIX } from "./constants";

const EMPTY = new Uint8Array(0);

interface Pubs {
  ikInitPub: string;
  ekInitPub: string;
  ikRespPub: string;
  ekRespPub: string;
}

function sessionKey(
  pubs: Pubs,
  dh1: Uint8Array,
  dh2: Uint8Array,
  dh3: Uint8Array,
  binding: Uint8Array,
): Uint8Array {
  const parts: Uint8Array[] = [SESSION_TAG];
  if (binding.length > 0) {
    parts.push(SESSION_BIND_PREFIX, binding);
  }
  parts.push(
    dhPubBytes(pubs.ikInitPub),
    dhPubBytes(pubs.ekInitPub),
    dhPubBytes(pubs.ikRespPub),
    dhPubBytes(pubs.ekRespPub),
    dh1,
    dh2,
    dh3,
  );
  return sha256(...parts);
}

/** Session key from the initiator (first-PUT) side. */
export function sessionKeyInitiator(
  ikInitPriv: string,
  ekInitPriv: string,
  pubs: Pubs,
  binding: Uint8Array = EMPTY,
): Uint8Array {
  return sessionKey(
    pubs,
    dhSharedBytes(ikInitPriv, pubs.ekRespPub),
    dhSharedBytes(ekInitPriv, pubs.ikRespPub),
    dhSharedBytes(ekInitPriv, pubs.ekRespPub),
    binding,
  );
}

/** Session key from the responder (answering) side. */
export function sessionKeyResponder(
  ikRespPriv: string,
  ekRespPriv: string,
  pubs: Pubs,
  binding: Uint8Array = EMPTY,
): Uint8Array {
  return sessionKey(
    pubs,
    dhSharedBytes(ekRespPriv, pubs.ikInitPub),
    dhSharedBytes(ikRespPriv, pubs.ekInitPub),
    dhSharedBytes(ekRespPriv, pubs.ekInitPub),
    binding,
  );
}

export type { Pubs };
