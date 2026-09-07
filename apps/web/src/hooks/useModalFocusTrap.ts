import { useEffect, type RefObject } from "react";

interface UseModalFocusTrapOptions {
  modalRef: RefObject<HTMLElement | null>;
  initialFocusRef?: RefObject<HTMLElement | null>;
  onClose: () => void;
  isDisabled?: boolean;
}

export function useModalFocusTrap({
  modalRef,
  initialFocusRef,
  onClose,
  isDisabled = false,
}: UseModalFocusTrapOptions) {
  useEffect(() => {
    const previousActiveElement = document.activeElement as HTMLElement | null;

    if (initialFocusRef?.current) {
      initialFocusRef.current.focus();
    } else if (modalRef.current) {
      const firstFocusable = modalRef.current.querySelector<HTMLElement>(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
      );
      firstFocusable?.focus();
    }

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        if (!isDisabled) {
          onClose();
        }
        return;
      }

      if (e.key === "Tab" && modalRef.current) {
        const focusableElements =
          modalRef.current.querySelectorAll<HTMLElement>(
            'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
          );
        const focusable = Array.from(focusableElements).filter(
          (el) => !el.hasAttribute("disabled") && el.offsetParent !== null,
        );

        if (focusable.length === 0) return;

        const firstElement = focusable[0]!;
        const lastElement = focusable[focusable.length - 1]!;

        if (e.shiftKey) {
          if (document.activeElement === firstElement) {
            e.preventDefault();
            lastElement.focus();
          }
        } else {
          if (document.activeElement === lastElement) {
            e.preventDefault();
            firstElement.focus();
          }
        }
      }
    };

    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
      if (
        previousActiveElement &&
        typeof previousActiveElement.focus === "function"
      ) {
        previousActiveElement.focus();
      }
    };
  }, [modalRef, initialFocusRef, onClose, isDisabled]);
}
