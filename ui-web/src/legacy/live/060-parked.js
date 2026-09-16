/* ---- in-flight turns survive session switches ----------------------
   A conversation holds its own turn now, and the registry's residency rule
   decides whether it keeps it while it is off screen
   (ui-web/src/state/session/residency.ts). The detached lane host the
   transcript island asks about is one of the things it keeps, so there is no
   parked-turn map and nothing for this part to install. */

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {

}
