"""Top-level domains accepted when recognising hosts and e-mail addresses.

A deliberately conservative subset of the IANA list: common country codes and
generic TLDs. Rare TLDs that collide with file extensions or ordinary words
(e.g. .zip, .py, .parts) are left out to avoid treating file names as hosts.
"""
TLDS = frozenset('academy ae aero agency ai app ar at au be bg biz blog br by ca cc ch cl cloud club cn co com company coop cz de design dev digital dk edu education ee engineering es eu fi fr global gov gr group hk hr hu id ie il in industries info institute int international io ir is it jp kr lt lu lv me media mil museum mx my net network news nl no nz online org pe ph pk pl pro pt ro rs ru sa science se sg shop si site sk software solutions space store studio su systems team tech technology th tools tr tv tw ua uk university us uy vn wiki works xyz za'.split())
