//! The lobby's wall time, optional UTC date override and packed STU dates.

use std::io;

pub const DAY: u64 = 86_400;
const NOON: u64 = DAY / 2;
const EPOCH_DAY: u64 = days_before_year(1970);
const STU_EPOCH_YEAR: u64 = 2000;
const FIRST_UNREPRESENTABLE_YEAR: u64 = 10_000;
const MONTH_LENGTHS: [u64; 12] = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];

/// The configured date means noon UTC. An empty setting or "now" uses wall time.
pub fn server_time(setting: &str, wall_time: u64) -> io::Result<u64> {
    let setting = setting.trim();
    if setting.is_empty() || setting.eq_ignore_ascii_case("now") {
        return Ok(wall_time);
    }
    let end = setting
        .char_indices()
        .nth(10)
        .map_or(setting.len(), |(end, _)| end);
    let (year, month_day) = setting[..end].split_once('-').ok_or_else(invalid_date)?;
    let (month, day) = month_day.split_once('-').ok_or_else(invalid_date)?;
    if year.len() != 4 || month.len() > 2 || day.len() > 2 {
        return Err(invalid_date());
    }
    let year = component(year)?;
    let month = component(month)?;
    let day = component(day.trim_start_matches(' '))?;
    let date = date_day(year, month, day).ok_or_else(invalid_date)?;
    Ok(date.checked_sub(EPOCH_DAY).ok_or_else(invalid_date)? * DAY + NOON)
}

fn component(text: &str) -> io::Result<u64> {
    if !text.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err(invalid_date());
    }
    text.parse().map_err(|_| invalid_date())
}

/// Pack UTC seconds into the unsigned STU year/month/day/hour/minute/second layout.
pub fn stu_datetime(seconds: u64) -> io::Result<u64> {
    let day = EPOCH_DAY + seconds / DAY;
    if !(days_before_year(STU_EPOCH_YEAR)..days_before_year(FIRST_UNREPRESENTABLE_YEAR))
        .contains(&day)
    {
        return Err(invalid_date());
    }
    let mut year = STU_EPOCH_YEAR;
    let mut next_year = FIRST_UNREPRESENTABLE_YEAR;
    while next_year - year > 1 {
        let middle = (year + next_year) / 2;
        if days_before_year(middle) > day {
            next_year = middle;
        } else {
            year = middle;
        }
    }
    let mut day_in_year = day - days_before_year(year);
    let mut month = 1;
    for length in month_lengths(year) {
        if day_in_year < length {
            break;
        }
        day_in_year -= length;
        month += 1;
    }
    let day_in_month = day_in_year + 1;
    let hour = (seconds % DAY) / 3600;
    let minute = (seconds % 3600) / 60;
    let second = seconds % 60;
    Ok(((year - STU_EPOCH_YEAR) << 36)
        | (month << 32)
        | (day_in_month << 27)
        | (hour << 22)
        | (minute << 16)
        | (second << 10))
}

/// Unpack a STU UTC timestamp. The bottom ten bits carry no date components.
pub fn from_stu_datetime(value: u64) -> io::Result<u64> {
    let year = STU_EPOCH_YEAR + (value >> 36);
    let month = (value >> 32) & 0x0f;
    let day = (value >> 27) & 0x1f;
    let hour = (value >> 22) & 0x1f;
    let minute = (value >> 16) & 0x3f;
    let second = (value >> 10) & 0x3f;
    // The reference timegm normalizes components relative to the month's first day.
    let month_start = date_day(year, month, 1).ok_or_else(invalid_date)?;
    let epoch_day = month_start - EPOCH_DAY + day - 1;
    Ok(epoch_day * DAY + hour * 3600 + minute * 60 + second)
}

const fn days_before_year(year: u64) -> u64 {
    let previous = year - 1;
    previous * 365 + previous / 4 - previous / 100 + previous / 400
}

fn month_lengths(year: u64) -> [u64; 12] {
    let mut lengths = MONTH_LENGTHS;
    let leap = year % 4 == 0 && (year % 100 != 0 || year % 400 == 0);
    lengths[1] += u64::from(leap);
    lengths
}

fn date_day(year: u64, month: u64, day: u64) -> Option<u64> {
    if !(1..FIRST_UNREPRESENTABLE_YEAR).contains(&year) || !(1..=12).contains(&month) {
        return None;
    }
    let month = month as usize - 1;
    let lengths = month_lengths(year);
    (1..=lengths[month])
        .contains(&day)
        .then(|| days_before_year(year) + lengths[..month].iter().sum::<u64>() + day - 1)
}

fn invalid_date() -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, "invalid lobby UTC date")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn configured_date_and_wall_time_match_python_at_the_dashboard_limits() {
        for (setting, expected) in [
            ("", 1_700_000_000),
            (" Now ", 1_700_000_000),
            ("2000-01-03", 946_900_800),
            ("2024-02-29", 1_709_208_000),
            ("2106-02-06", 4_294_900_800),
            ("2026-09-29T09:00", 1_790_683_200),
            ("2024-2-9", 1_707_480_000),
        ] {
            assert_eq!(server_time(setting, 1_700_000_000).unwrap(), expected);
        }
        for invalid in [
            "2100-02-29",
            "2024-04-31",
            "2024-00-02",
            "0000-01-01",
            "bad",
        ] {
            assert!(server_time(invalid, 0).is_err());
        }
    }

    #[test]
    fn stu_components_match_python_before_and_after_signed_unix_rollover() {
        for (seconds, packed) in [
            (946_684_800, 0x001_0800_0000),
            (1_700_000_000, 0x17b_758d_5000),
            (1_709_164_800, 0x182_e800_0000),
            (2_147_483_648, 0x261_98ce_2000),
            (4_294_967_295, 0x6a2_399c_3c00),
        ] {
            assert_eq!(stu_datetime(seconds).unwrap(), packed);
            assert_eq!(from_stu_datetime(packed | 0x3ff).unwrap(), seconds);
        }
        assert!(stu_datetime(946_684_799).is_err());
        assert!(from_stu_datetime(0).is_err());
    }
}
