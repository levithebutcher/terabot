import random
import time

# Cooldown tracker to prevent Telegram FloodWait from text spammers
_user_joke_cooldown: dict[int, float] = {}
COOLDOWN_SECONDS = 5.0

NONVEG_JOKES = [
    (
        "Doctor: 'Aapki biwi ko aaram ki zaroorat hai, roz raat ko inke pair dabaya karo.'\n"
        "Pappu: 'Doctor sahab, thoda aur upar dabau toh?'\n"
        "Doctor: 'Bhai aaram biwi ko dilana hai, khud ko nahi!' 💀🤣"
    ),
    (
        "Teacher: 'Beta Pappu, aisi kaunsi cheez hai jo ladkiyo ki saal me ek baar badhti hai par ladko ki roz badhti hai?'\n"
        "Pappu: 'Daadhi-mooch madam!'\n"
        "Teacher: 'Tu baith ja besharam, dimaag hamesha gutter me hi rehta hai tera!' 😂"
    ),
    (
        "Girlfriend: 'Baby, aaj kuch toofani karte hain na?'\n"
        "Boyfriend (excited): 'Haan bolo kya karna hai?'\n"
        "Girlfriend: 'Pehle saare kapde utaaro...'\n"
        "Boyfriend: 'Fir??'\n"
        "Girlfriend: 'Fir Surf Excel daal kar dho daalo, subah pehenna bhi hai!' 😭😂"
    ),
    (
        "Pappu ek ladki se: 'Agar main tumhare kapde khol doon toh tum kya karogi?'\n"
        "Ladki: 'Thappad marungi besharam!'\n"
        "Pappu: 'Arrey chhat pe sukh rahe hain, baarish aane wali hai! Ganda dimaag leke ghoomti ho!' ☔🤣"
    ),
    (
        "Biwi: 'Suniye ji, padosan ke pati ko dekho, roz office jaane se pehle biwi ko kiss karke jaata hai. Aap kyu nahi karte?'\n"
        "Pati: 'Arrey main toh kar lu, par padosan bura maan gayi toh?' 🤕😂"
    ),
    (
        "Doctor: 'Subah-shaam 1 goli paani ke sath lena, aur sone se pehle lena.'\n"
        "Pappu: 'Kiske sath doctor sahab?'\n"
        "Doctor: 'Biwi ke sath bewaqoof! Padosan ke sath nahi!' 💊🤣"
    ),
    (
        "Ladki: 'Tumhara sabse bada sapna kya hai?'\n"
        "Ladka: 'Ek aisi car jisme kaale parde lage ho aur sunsaan road ho!'\n"
        "Ladki: 'Haww... fir kya karoge?'\n"
        "Ladka: 'Chain ki neend sounga, 3 din se soya nahi hoon!' 🚗😴"
    ),
    (
        "Girlfriend: 'Baby, jab tum mere paas aate ho toh tumhari saansein itni tez kyu chalne lagti hain?'\n"
        "Boyfriend: 'Kyunki pet andar kheench kar rakhna padta hai na moti!' 😤😂"
    ),
    (
        "Pappu: 'Yaar meri biwi ko lagta hai main use time nahi deta.'\n"
        "Dost: 'Toh use time diya kar na bhai!'\n"
        "Pappu: 'Arrey deta toh hoon, par 2 minute me khatam ho jata hai!' ⏰💀😂"
    ),
    (
        "Biwi: 'Shaadi se pehle toh tum kehte the ki chaand taare tod kar launga, ab kya hua?'\n"
        "Pati: 'Toh tab kya mujhe pata tha ki din-raat tum hi mera sar todogi!' 💥🤣"
    ),
    (
        "Ladki: 'Suno, main pregnant hoon!'\n"
        "Ladka: 'Par humne toh protection use kiya tha?'\n"
        "Ladki: 'Woh thoda phat gaya tha...'\n"
        "Ladka: 'Tabhi sochu Made in China kyu likha tha uspe!' 🤦‍♂️💀"
    ),
    (
        "Pappu ek doctor ke paas gaya:\n"
        "Pappu: 'Doctor sahab, jab main soota hoon toh sapne me ladkiyan aati hain aur wrestling karti hain!'\n"
        "Doctor: 'Toh ye goli le lo, aaj raat se nahi aayengi.'\n"
        "Pappu: 'Kal se lu doctor sahab? Aaj final match hai!' 🏆🤣"
    ),
    (
        "Ek ladka chemist ki dukaan par gaya:\n"
        "Ladka: 'Bhaiya, ek aisa condom do jisme koi aawaz na ho.'\n"
        "Chemist: 'Bhai chalana hai ya silent mode pe ring bajani hai?' 🔇😂"
    ),
    (
        "Pappu: 'Bhai shaadi ke baad mard ka dimaag kyu badal jaata hai?'\n"
        "Dost: 'Kyunki hardware wahi rehta hai par operating system biwi chalaane lagti hai!' 💻🤣"
    ),
    (
        "Girlfriend: 'Agar main doobne lagi toh tum mujhe bachaoge?'\n"
        "Boyfriend: 'Pehle ye batao, agar main bacha lu toh shaadi karogi?'\n"
        "Girlfriend: 'Nahi!'\n"
        "Boyfriend: 'Toh phir tairna seekh lo, mujhe swimming nahi aati!' 🏊‍♂️😂"
    ),
]


def get_random_joke() -> str:
    """Return a random funny desi double-meaning / non-veg joke."""
    return random.choice(NONVEG_JOKES)


def can_send_joke(user_id: int) -> bool:
    """Enforce a cooldown so text spammers don't trigger Telegram FloodWait."""
    now = time.monotonic()
    last = _user_joke_cooldown.get(user_id, 0.0)
    if now - last < COOLDOWN_SECONDS:
        return False
    _user_joke_cooldown[user_id] = now
    return True
