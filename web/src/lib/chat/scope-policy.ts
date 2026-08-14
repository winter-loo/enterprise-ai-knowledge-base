export type ChatScope = {
	kbId: string;
	projectId: string;
	department: string;
};

/**
 * A scope is part of a conversation's identity. Returning true starts a fresh
 * session so messages retrieved under one permission boundary are never sent
 * back as context for another boundary.
 *
 * This is the product-policy seam: the conservative implementation treats all
 * three fields as retrieval boundaries. A future authenticated backend could
 * narrow this comparison if some controls become presentation-only.
 */
export function shouldStartNewSession(previous: ChatScope, next: ChatScope): boolean {
	return (
		previous.kbId !== next.kbId ||
		previous.projectId !== next.projectId ||
		previous.department !== next.department
	);
}
