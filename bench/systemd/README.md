# Pi units, and the two things that are easy to get wrong

## `nofail` on the artifact drive is not optional

§8.4. Without it, a Pi booted at the bench with the drive unplugged drops to an
emergency shell — a brick, on a machine with no keyboard. In `/etc/fstab`:

```
UUID=<the-drive-uuid>  /mnt/bench-store  ext4  defaults,nofail,x-systemd.device-timeout=10  0  2
```

State ext4 explicitly. FAT32 fails at 4 GiB with `EFBIG`, and a firmware dump
crosses that.

## Do NOT add `RequiresMountsFor` to the worker unit

Also §8.4, and it reads like the right thing to do. With the drive absent the
unit never starts, the port never listens, and `/worker list` says
`UNREACHABLE` — **the same string it prints for "the Pi is off."** Start
unconditionally and fail at call time with a message naming the mountpoint.

## Why the status page depends on `network.target`, not `network-online.target`

The page exists to report that the network is down. A unit that waits for the
network to be up cannot do that.
